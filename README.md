# Smart Locker Cabinet · 智能借还柜

一台自助借还柜的服务端：员工扫描格口二维码借/还手持终端（PDA），柜面大屏实时显示每个格口的状态，ESP32 主控通过 HTTP 轮询驱动电控锁。**零第三方依赖**——纯 Python 标准库 + SQLite，`python3 server.py` 即可运行。

> A self-service locker cabinet server for shared handheld devices (PDAs): scan a QR code on a compartment to borrow or return a device, watch live status on a big screen, and let an ESP32 controller drive the electric locks over HTTP polling. Zero third-party dependencies: Python standard library + SQLite only.

## 功能清单

- **10 格口状态机**：在位可借 / 开锁中 / 已借出 / 归还中，状态由硬件上报自动推进
- **扫码借还页**：每格一个二维码 URL，员工在手机浏览器里点「借出 / 归还」即可
- **状态大屏**：柜面浏览器全屏打开 `/screen`，SSE 实时刷新，异常（门超时未关）红字告警
- **飞书免登（可选）**：配置后扫码自动识别员工身份；未配置时降级为手工输入姓名 + 工号，不阻断流程
- **在位检测与门磁**：取出/放回由传感器确认后才记账，开门超过 120 秒自动蜂鸣告警
- **借还流水**：每次借出/归还都落库，可通过 API 查询
- **机械钥匙应急**：断网/断电时柜门可由机械钥匙打开（硬件侧保留）

## 架构一句话

服务端（Python + SQLite）只负责状态、身份和指令队列；ESP32 每 2 秒轮询取开锁指令并上报在位/门磁状态；员工手机页与柜面大屏都是服务端直出的静态页面，无构建步骤。

## 快速开始

```bash
cd smart-locker-cabinet
python3 server.py 8080        # 默认端口 8080，可省略
```

启动后：

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 扫码借还页 | `http://<服务器地址>:8080/box/3` | 3 号格；每格二维码就印这个 URL |
| 状态大屏 | `http://<服务器地址>:8080/screen` | 柜面浏览器全屏打开 |
| 健康检查 | `GET /healthz` | 返回 `{"ok": true, ...}` |
| 借还记录 | `GET /api/log?limit=50` | 最近借还流水 |

数据库 `cabinet.db` 在首次启动时按 `schema.sql` 自动建表，无需手工初始化。

## 飞书免登配置（可选）

配好后，员工用飞书扫格口二维码即自动识别身份（姓名 + 用户 ID），无需手工填写。方法：

1. 在[飞书开放平台](https://open.feishu.cn/)创建一个**网页应用**，获得 App ID 与 App Secret；
2. 按飞书「网页应用免登」文档配置好可信域名/重定向，使扫码打开借还页时能带回免登 `code`；
3. 启动服务端时通过环境变量注入凭证（**不要把真实凭证写进代码或提交进仓库**）：

   ```bash
   FEISHU_APP_ID=<your-app-id> FEISHU_APP_SECRET=<your-app-secret> python3 server.py 8080
   ```

4. 未设置这两个环境变量时，免登自动关闭，借还页降级为手工输入姓名 + 工号，全部功能照常可用。

> 飞书免登要求页面通过 HTTPS 可信域名访问。正式部署建议在服务端前面加一层反向代理（如 Nginx）终止 TLS；直连 IP + HTTP 仅适合本地试运行。

## ESP32 对接协议（HTTP 短轮询）

选用 HTTP 短轮询而非 MQTT：ESP32 端实现简单，无需额外部署消息中间件，2 秒轮询的实时性已足够。

- 取指令（每 2 秒）：`GET /api/poll?device=cab1`
  → `{"commands":[{"id":12,"box_id":3,"action":"unlock"}], ...}`
- 执行确认：`POST /api/ack`，body：`{"cmd_id":12,"ok":true}`
- 状态上报（在位/门磁变化时，或每 10 秒心跳）：`POST /api/report`，
  body：`{"box":3,"present":1,"door_closed":1}`

服务端按上报自动推进状态机：

- `unlocking_borrow` + 在位 1→0 → 写借出流水 → `borrowed`
- `unlocking_return` + 在位 0→1 → 写归还流水 → `idle`
- 门打开超过 120 秒 → 下发 `beep` 指令，格口标记异常，大屏红字提示

## 硬件说明（概要）

- **主控**：ESP32 开发板，负责轮询指令、驱动锁具、采集传感器
- **锁具**：每格一把 12V 电控柜锁（通电开锁型，建议带锁状态反馈与机械钥匙应急孔）
- **驱动**：ESP32 的 GPIO 不能直驱锁线圈，每路须经 MOS 管模块（或继电器模块）驱动，并做好续流保护
- **检测**：每格在位检测（红外对管或微动开关）+ 门磁开关，各占一路 GPIO
- **其他**：每格 USB 充电口（给存放的终端补电）、蜂鸣器（超时告警）、柜面平板/显示器（开浏览器全屏显示大屏页）

> 仓库内目前只包含服务端；ESP32 参考固件与接线图在规划中，见 [ROADMAP.md](ROADMAP.md)。

## 数据说明

- 数据存本地 SQLite 文件 `cabinet.db`，结构见 `schema.sql`：
  `boxes`（格口状态）/ `borrow_log`（借还流水）/ `commands`（下发给主控的指令队列）
- 数据库文件已在 `.gitignore` 中排除；备份时拷走该文件即可

## 安全提示

- 格口二维码是静态 URL，身份真实性依赖免登登录态；生产环境建议只启用免登模式并关闭手工输入（见 ROADMAP）
- 借还页与 API 目前没有额外的访问鉴权，请部署在受控网络内，或通过反向代理加访问控制
- 凭证只通过环境变量注入，切勿硬编码

## 目录结构

```
smart-locker-cabinet/
├── server.py     # 服务端主程序（HTTP 服务 + 状态机 + 飞书免登）
├── box.html      # 员工扫码借还页（模板，服务端渲染）
├── screen.html   # 柜面状态大屏页（SSE 实时刷新）
├── schema.sql    # SQLite 表结构（启动时自动建表）
├── README.md
├── ROADMAP.md
└── LICENSE
```

## English (brief)

Smart Locker Cabinet is the server for a self-service device-lending cabinet with 10 compartments. Employees scan the QR code on a compartment to borrow or return a handheld terminal; a big-screen page shows live compartment status; an ESP32 controller polls the server over HTTP to drive 12 V electric locks and report presence/door sensors. Built with the Python standard library and SQLite only — run it with `python3 server.py 8080`, no installation needed. Optional Feishu (Lark) passwordless login identifies employees automatically when credentials are provided via the `FEISHU_APP_ID` / `FEISHU_APP_SECRET` environment variables; without them, the UI falls back to manual name/ID entry. See the Chinese sections above for the ESP32 protocol and hardware notes.

## License

[MIT](LICENSE) © Smart Locker Cabinet contributors
