#!/usr/bin/env python3
"""Smart Locker Cabinet · 智能借还柜服务器程序（v0.1，可运行）

零第三方依赖：只用 Python 标准库（http.server + sqlite3 + urllib）。
部署：python3 server.py [port]   # 默认 8080，数据存在 ./cabinet.db

架构说明（见 README.md）：
- 员工用飞书扫格口二维码 -> 打开 /box/<n> -> 借/还
- 柜面大屏浏览器全屏打开 /screen（SSE 实时刷新）
- ESP32 主控走 HTTP 短轮询：GET /api/poll 取开锁指令，
  POST /api/ack 确认执行，POST /api/report 上报在位/门磁状态
"""

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cabinet.db")
N_BOXES = 10
DOOR_TIMEOUT_SEC = 120  # 开门超时未关门 -> 告警

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

_cst = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(_cst).isoformat(timespec="seconds")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        schema = f.read()
    conn = get_db()
    conn.executescript(schema)
    n = conn.execute("SELECT COUNT(*) c FROM boxes").fetchone()["c"]
    if n == 0:
        for i in range(1, N_BOXES + 1):
            conn.execute(
                "INSERT INTO boxes (id, pda_no, updated_at) VALUES (?,?,?)",
                (i, f"PDA-{i:02d}", now_iso()),
            )
    conn.commit()
    conn.close()


# ---------------- 飞书身份 ----------------
def feishu_user_from_code(code):
    """飞书网页应用免登：用 url 上的 code 换取用户身份。
    未配置 App ID/Secret 时返回 None，调用方降级为手工输入工号模式。"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and code):
        return None
    try:
        # 1. 拿 tenant_access_token
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        token = json.load(urllib.request.urlopen(req, timeout=10))["tenant_access_token"]
        # 2. code -> user
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/authen/v1/index",
            data=json.dumps({"grant_type": "authorization_code", "code": code}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        data = json.load(urllib.request.urlopen(req, timeout=10))["data"]
        return {"name": data.get("name", ""), "emp_id": data.get("user_id", "")}
    except Exception as e:  # noqa: BLE001 - 免登失败就降级，不阻断借还
        print(f"[feishu] auth failed: {e}")
        return None


# ---------------- 业务逻辑 ----------------
def box_status(conn):
    rows = conn.execute("SELECT * FROM boxes ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def broadcast():
    """通知所有 SSE 客户端刷新（简单实现：靠客户端定时拉，见 /api/events）。"""
    pass


def do_borrow(conn, box_id, user, emp_id):
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        return False, "格口不存在"
    if box["state"] != "idle" or not box["present"]:
        return False, "该格 PDA 不在位或正在处理中"
    if not user:
        return False, "请先确认身份"
    ts = now_iso()
    conn.execute(
        "INSERT INTO commands (box_id, action, created_at) VALUES (?,?,?)",
        (box_id, "unlock", ts),
    )
    conn.execute(
        "UPDATE boxes SET state='unlocking_borrow', borrower=?, emp_id=?, "
        "borrow_time=?, alert='', updated_at=? WHERE id=?",
        (user, emp_id, ts, ts, box_id),
    )
    conn.commit()
    return True, f"{box_id} 号格已弹开，请取走 {box['pda_no']} 并关好门"


def do_return(conn, box_id, user, emp_id):
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        return False, "格口不存在"
    if box["state"] != "borrowed":
        return False, "该格当前不是借出状态，无需归还"
    if box["emp_id"] and emp_id and box["emp_id"] != emp_id:
        return False, f"该 PDA 是 {box['borrower']} 借走的，请由本人归还"
    ts = now_iso()
    conn.execute(
        "INSERT INTO commands (box_id, action, created_at) VALUES (?,?,?)",
        (box_id, "unlock", ts),
    )
    conn.execute(
        "UPDATE boxes SET state='unlocking_return', alert='', updated_at=? WHERE id=?",
        (ts, box_id),
    )
    conn.commit()
    return True, f"{box_id} 号格已弹开，请将 {box['pda_no']} 放回（充电口插好）并关好门"


def esp_report(conn, box_id, present, door_closed):
    """ESP32 上报在位/门磁。返回 (changed: bool)。"""
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        return False
    ts = now_iso()
    changed = False
    state, alert = box["state"], box["alert"]

    if state == "unlocking_borrow" and not present:
        # 借出：确认取走
        conn.execute(
            "INSERT INTO borrow_log (box_id, pda_no, user, emp_id, action, ts)"
            " VALUES (?,?,?,?,?,?)",
            (box_id, box["pda_no"], box["borrower"], box["emp_id"], "borrow", ts),
        )
        state, changed = "borrowed", True
    elif state == "unlocking_return" and present:
        # 归还：确认放回
        conn.execute(
            "INSERT INTO borrow_log (box_id, pda_no, user, emp_id, action, ts)"
            " VALUES (?,?,?,?,?,?)",
            (box_id, box["pda_no"], box["borrower"], box["emp_id"], "return", ts),
        )
        state = "idle"
        conn.execute(
            "UPDATE boxes SET borrower='', emp_id='', borrow_time='' WHERE id=?",
            (box_id,),
        )
        changed = True

    # 门超时告警
    if not door_closed:
        opened_at = getattr(esp_report, "_door_open", {}).get(box_id)
        if opened_at is None:
            getattr(esp_report, "_door_open", {})[box_id] = time.time()
        elif time.time() - opened_at > DOOR_TIMEOUT_SEC and alert != "door_timeout":
            alert = "door_timeout"
            conn.execute(
                "INSERT INTO commands (box_id, action, created_at)"
                " VALUES (?, 'beep', ?)",
                (box_id, ts),
            )
            changed = True
    else:
        getattr(esp_report, "_door_open", {}).pop(box_id, None)
        if alert == "door_timeout":
            alert, changed = "", True

    conn.execute(
        "UPDATE boxes SET present=?, door_closed=?, state=?, alert=?, updated_at=?"
        " WHERE id=?",
        (1 if present else 0, 1 if door_closed else 0, state, alert, ts, box_id),
    )
    conn.commit()
    return changed


esp_report._door_open = {}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SmartLockerCabinet/0.1"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _page(self, name):
        with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
            self._send(200, f.read(), "text/html")

    def _parse_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(raw or "{}")
        return dict(urllib.parse.parse_qsl(raw))

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            return self._json({"ok": True, "time": now_iso()})

        if path == "/":
            return self._page("screen.html")

        if path == "/screen":
            return self._page("screen.html")

        if path.startswith("/box/"):
            try:
                box_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._json({"ok": False, "msg": "格口号错误"}, 400)
            # 飞书免登：?code=xxx
            code = q.get("code", [""])[0]
            user = feishu_user_from_code(code)
            html = open(os.path.join(BASE_DIR, "box.html"), encoding="utf-8").read()
            conn = get_db()
            box = conn.execute(
                "SELECT * FROM boxes WHERE id=?", (box_id,)
            ).fetchone()
            conn.close()
            if not box:
                return self._json({"ok": False, "msg": "格口不存在"}, 404)
            html = html.replace("{{BOX_ID}}", str(box_id))
            html = html.replace("{{PDA_NO}}", box["pda_no"])
            html = html.replace("{{STATE}}", box["state"])
            html = html.replace("{{BORROWER}}", box["borrower"] or "")
            html = html.replace("{{BORROW_TIME}}", box["borrow_time"] or "")
            html = html.replace("{{FEISHU_NAME}}", (user or {}).get("name", ""))
            html = html.replace("{{FEISHU_EMP}}", (user or {}).get("emp_id", ""))
            html = html.replace(
                "{{FEISHU_OK}}", "1" if user else "0"
            )
            return self._send(200, html, "text/html")

        if path == "/api/status":
            conn = get_db()
            data = box_status(conn)
            conn.close()
            return self._json({"ok": True, "boxes": data, "time": now_iso()})

        if path == "/api/poll":
            # ESP32 取指令：/api/poll?device=cab1
            conn = get_db()
            rows = conn.execute(
                "SELECT id, box_id, action FROM commands WHERE status='pending'"
                " ORDER BY id"
            ).fetchall()
            conn.close()
            return self._json(
                {
                    "ok": True,
                    "commands": [dict(r) for r in rows],
                    "poll_interval_sec": 2,
                }
            )

        if path == "/api/events":
            # 大屏 SSE：每 3 秒推一次全量状态
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    conn = get_db()
                    data = box_status(conn)
                    conn.close()
                    payload = json.dumps(
                        {"boxes": data, "time": now_iso()}, ensure_ascii=False
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(3)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/log":
            limit = int(q.get("limit", ["50"])[0])
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM borrow_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return self._json({"ok": True, "log": [dict(r) for r in rows]})

        return self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._parse_body()

        if path == "/api/borrow":
            conn = get_db()
            ok, msg = do_borrow(
                conn,
                int(body.get("box", 0)),
                body.get("user", "").strip(),
                body.get("emp_id", "").strip(),
            )
            conn.close()
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/return":
            conn = get_db()
            ok, msg = do_return(
                conn,
                int(body.get("box", 0)),
                body.get("user", "").strip(),
                body.get("emp_id", "").strip(),
            )
            conn.close()
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/ack":
            # ESP32 确认指令已执行：{cmd_id, ok}
            conn = get_db()
            conn.execute(
                "UPDATE commands SET status='acked', acked_at=? WHERE id=?",
                (now_iso(), int(body.get("cmd_id", 0))),
            )
            conn.commit()
            conn.close()
            return self._json({"ok": True})

        if path == "/api/report":
            # ESP32 上报：{box, present: 0/1, door_closed: 0/1}
            conn = get_db()
            changed = esp_report(
                conn,
                int(body.get("box", 0)),
                bool(int(body.get("present", 1))),
                bool(int(body.get("door_closed", 1))),
            )
            conn.close()
            return self._json({"ok": True, "changed": changed})

        return self._json({"ok": False, "msg": "not found"}, 404)

    def log_message(self, fmt, *args):  # 安静日志，只打关键行
        if self.path.startswith(("/api/poll", "/api/events")):
            return
        print(f"[{now_iso()}] {self.command} {self.path}")


def main():
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[smart-locker-cabinet] serving on :{port}, db={DB_PATH}")
    print(f"[smart-locker-cabinet] 飞书免登: {'已配置' if FEISHU_APP_ID else '未配置（降级为手工输入工号）'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
