-- Smart Locker Cabinet · 数据库结构
-- SQLite，server.py 启动时自动执行建表

CREATE TABLE IF NOT EXISTS boxes (
  id          INTEGER PRIMARY KEY,   -- 格口号 1..10
  pda_no      TEXT NOT NULL,         -- 绑定的 PDA 编号，如 PDA-03
  present     INTEGER NOT NULL DEFAULT 1,  -- 在位检测：1 在位 / 0 已取走
  door_closed INTEGER NOT NULL DEFAULT 1,  -- 门磁：1 关好 / 0 未关
  state       TEXT NOT NULL DEFAULT 'idle',
                 -- idle: 在位可借 | unlocking_borrow: 借出开锁中
                 -- borrowed: 已借出 | unlocking_return: 归还开锁中
  borrower    TEXT DEFAULT '',       -- 借用人姓名
  emp_id      TEXT DEFAULT '',       -- 借用人标识（飞书 user_id 或工号）
  borrow_time TEXT DEFAULT '',       -- 借出时间（ISO）
  alert       TEXT DEFAULT '',       -- 异常标记：door_timeout 等，空=正常
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS borrow_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  box_id  INTEGER NOT NULL,
  pda_no  TEXT NOT NULL,
  user    TEXT NOT NULL,
  emp_id  TEXT NOT NULL,
  action  TEXT NOT NULL,             -- borrow / return
  ts      TEXT NOT NULL              -- ISO 时间
);

-- 下发给 ESP32 的指令队列（ESP32 短轮询取走，执行后 ack）
CREATE TABLE IF NOT EXISTS commands (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  box_id     INTEGER NOT NULL,
  action     TEXT NOT NULL,          -- unlock: 开锁 / beep: 蜂鸣告警
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending / acked
  created_at TEXT NOT NULL,
  acked_at   TEXT
);
