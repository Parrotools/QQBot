# 生产部署与运行手册

## 运行前检查

- 使用 Python 3.11+，在项目根目录复制 `.env.example` 为 `.env`。
- `.env` 只保存在部署机器，不能提交到 Git；至少填写 `ONEBOT_ACCESS_TOKEN`、`LLM_API_KEY`、`ADMIN_QQ_IDS`。
- 默认监听 `127.0.0.1:8080`。NapCat 和 Bot 在不同机器时，设置 `HOST=0.0.0.0` 并用防火墙只放行 NapCat 所在主机。

## Windows

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
.venv\Scripts\python bot.py
```

建议用任务计划程序或 NSSM 以工作目录设为项目根目录运行上述命令。停止前发送正常终止信号，避免强制杀进程。

## Linux

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python bot.py
```

生产环境可用 systemd，`WorkingDirectory` 指向项目根目录，`ExecStart` 使用 `.venv/bin/python bot.py`，并设置 `Restart=on-failure`。日志由 systemd 或进程管理器收集。

## NapCat OneBot 11

在 NapCat WebUI 创建 WebSocket 客户端：

| 字段 | 值 |
|---|---|
| `enable` | `true` |
| `url` | `ws://<bot-host>:<PORT>/onebot/v11/ws` |
| `token` | 与 `ONEBOT_ACCESS_TOKEN` 完全一致 |
| `reconnectInterval` | `5000` 或 NapCat 默认值 |

NapCat 是客户端，Bot 是服务端。两者同机时使用 `ws://127.0.0.1:8080/onebot/v11/ws`。Bot 启动后在 NapCat 日志确认 WebSocket connected，再用另一个 QQ 私聊发送“你好”。

## 环境变量

除 `.env.example` 的网页、并发、Scheduler 配置外，长期运行重点关注：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `DATABASE_PATH` | `data/bot.db` | SQLite 数据库路径 |
| `GITHUB_CHECK_CRON` | `0 * * * *` | GitHub 检查频率；留空关闭 |
| `DAILY_REPORT_CRON` | `0 23 * * *` | 日报时间；留空关闭 |
| `OUTBOUND_MAX_ATTEMPTS` | `3` | 主动通知最大投递次数 |
| `OUTBOUND_RETRY_DELAY_SECONDS` | `30` | 投递失败后的固定等待秒数 |
| `OUTBOUND_QUEUE_POLL_SECONDS` | `1` | 队列空闲轮询秒数 |
| `OUTBOUND_LEASE_SECONDS` | `60` | 进程异常后 `sending` 任务可被重新领取的秒数 |

提醒、GitHub 变化和日报先写入 SQLite 队列，再由 Dispatcher 投递。进程重启不会丢失未完成投递；超过最大次数的记录会进入 `failed` 状态。

## 健康检查、备份与升级

- 访问 `GET /healthz`：200 表示数据库、LLM、GitHub API 均可用；503 表示至少一项失败。不要将该端点暴露到公网。
- QQ 内发送 `/status` 可查看运行时间、依赖状态、任务数、监控数、记忆数和最近投递错误。
- 升级前先停止 Bot，并备份整个 `data/` 目录。SQLite 使用 WAL 时应同时备份 `bot.db`、`bot.db-wal`、`bot.db-shm`，或先正常停机后仅备份 `bot.db`。
- 新版本启动时自动执行有序 migration。已有数据不会被删除；若启动失败，保留备份并查看 migration 相关日志，不要手改 `schema_migrations`。

## 故障排查

| 现象 | 排查方式 |
|---|---|
| NapCat 持续重连 | 检查 `url` 路径、端口、防火墙和双方 token 是否一致 |
| `/healthz` 的 LLM 为异常 | 检查 `LLM_BASE_URL`、API Key、模型权限和出网；错误详情只看部署日志 |
| GitHub 为异常 | 检查出网、`GITHUB_TOKEN` 与 API 限额；无 token 时限额较低 |
| 主动通知未送达 | 用 `/status` 查看最近错误；检查 `outbound_messages` 中 `status/attempts/last_error`，确认 NapCat 已连接 |
| 定时任务不运行 | 核对时区与 cron，重启后用 `/status` 确认 Scheduler 运行中及任务数 |
| 数据库锁定 | 确认只运行一个 Bot 进程；正常停止残留进程后恢复，禁止多个实例共用同一 SQLite 文件 |
