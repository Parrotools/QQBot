# qq-llm-bot

用真实 QQ 账号运行的 LLM 聊天机器人：NapCatQQ + OneBot 11 + NoneBot2 + OpenAI Compatible API。

功能一览：

- 私聊 / 群聊 @ / 回复机器人续聊，群内普通消息不响应，未知命令给帮助而不是丢给 LLM
- 多轮上下文会话（SQLite 持久化，群内成员默认独立上下文），`/clear` 清空
- YAML 可配置人格（默认 Rumi）
- 用户显式维护的长期记忆，并在聊天时按重要度加载
- 一次性与 cron 定时提醒（通知默认关闭）
- GitHub 仓库监控：手动检查、commit/Star/Fork/Issue/Release 变化通知
- 每日日报：汇总 GitHub 变化、任务/提醒和重要记忆，可手动查看
- 网页总结：识别消息中的 URL，抓取正文，超长网页 Map-Reduce 分块总结；URL 结果进程内缓存（TTL 可配）；可选 Playwright 渲染兜底
- 主动发送 / 多目标群发（仅管理员，带预览确认、TTL、限速、逐目标结果记录、旧待确认任务自动作废）
- 安全：SSRF 逐跳校验、Prompt Injection 定界防御、LLM 无任何发送能力、API Key 不入日志

> 详细命令用法和操作流程见 [USAGE.md](USAGE.md)。
> 长期运行、升级、备份和故障排查见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 架构

```
真实 QQ 账号
   │
   ▼
NapCatQQ                    ← 只负责 QQ 协议 ↔ OneBot 11 转换
   │  OneBot 11 反向 WebSocket（NapCat 主动连入 Bot）
   ▼
NoneBot2 (FastAPI 驱动, ws://127.0.0.1:8080/onebot/v11/ws)
   │
   ├─ app/personality/    YAML 人格配置与加载
   ├─ app/plugins/        事件路由：ai_chat / web_summary / memory / scheduler / github / report / broadcast / admin
   ├─ app/services/       业务层：LLM Gateway、网页管道、会话、记忆、Scheduler、GitHub、Report、MessageDispatcher
   ├─ app/security/       权限、SSRF、Prompt Injection 防御
   └─ app/database/       SQLite (aiosqlite)：sessions / messages / memories / scheduled_tasks / GitHub 快照 / send_logs
```

## 环境要求

- Python 3.11+
- Windows 10/11 或 Linux（x86_64）
- 一个真实 QQ 号（建议用小号，挂协议有风控风险）
- 任一 OpenAI Compatible API 的 Key（GLM / DeepSeek / OpenAI / Qwen / vLLM 均可）

## 快速开始（5 步）

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv/Scripts/pip install -e .        # Windows
# .venv/bin/pip install -e .          # Linux

# 2. 配置
copy .env.example .env                # Windows（Linux: cp）
#   编辑 .env：填 LLM_API_KEY、自定 ONEBOT_ACCESS_TOKEN、ADMIN_QQ_IDS

# 3. 安装并登录 NapCatQQ（见下文第 5-7 节）

# 4. 启动
.venv/Scripts/python bot.py

# 5. 用另一个 QQ 号私聊机器人发"你好"验证链路
```

## 1. Python 环境安装

Windows（PowerShell）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
```

Linux：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

也可以用 uv：`uv venv && uv pip install -e ".[dev]"`。

## 2. 安装依赖

```bash
pip install -e .          # 运行依赖
pip install -e ".[dev]"   # 含 pytest / ruff
```

## 3. 配置 .env

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

然后编辑 `.env`，至少修改三处：

| 变量 | 说明 |
|---|---|
| `ONEBOT_ACCESS_TOKEN` | 自定一个随机字符串，**必须与 NapCat 侧填的完全一致** |
| `LLM_API_KEY` | 你的 LLM API Key |
| `LLM_BASE_URL` / `LLM_MODEL` | 按厂商填，如 GLM：`https://open.bigmodel.cn/api/paas/v4` + `glm-4-flash` |
| `PERSONALITY_FILE` | 人格 YAML 文件路径，默认 `app/personality/rumi.yaml` |
| `OWNER_QQ_ID` | 主人的真实 QQ 号；只按此 ID 识别主人，不按昵称猜测（留空且只有一个管理员时回退到该 ID） |
| `OWNER_NAME` | 主人称呼，默认 `Parrotools` |
| `NON_LLM_REPLY_DELAY_SECONDS` | 不调用 LLM 的交互回复等待秒数，默认 2；设为 0 关闭 |

其余变量（限速、并发、网页大小上限、URL 缓存等）默认值即可运行，详见 `.env.example` 注释。

## 4. NapCatQQ 安装

- Windows：下载 [NapCat.Shell](https://github.com/NapNeko/NapCatQQ/releases)（zip 解压即用，无需安装完整 QQ）或使用 NapCat.Installer。
- Linux：推荐 Docker（`mlikiowa/napcat-docker` 镜像）或一键安装脚本（见 NapCatQQ 官方文档 https://napcat.napneko.icu/guide/start-install ）。

## 5. 登录 QQ

启动 NapCat 后控制台会出现二维码，用机器人 QQ 号扫码登录（首次登录后保留会话）。登录成功后进入 NapCat WebUI（默认 `http://127.0.0.1:6099/webui`，token 见启动日志）。

## 6. NapCat OneBot 11 反向 WebSocket 配置

在 NapCat WebUI → 网络配置 → 新建「WebSocket 客户端」（websocketClients）：

| 字段 | 填写值 |
|---|---|
| enable | `true` |
| url | `ws://127.0.0.1:8080/onebot/v11/ws` |
| token | 与 `.env` 中 `ONEBOT_ACCESS_TOKEN` **完全相同** |
| heartInterval | 默认即可 |

对应 JSON（可直接改 NapCat 的 onebot11 配置文件）：

```json
{
  "network": {
    "websocketClients": [
      {
        "enable": true,
        "url": "ws://127.0.0.1:8080/onebot/v11/ws",
        "token": "your-onebot-token",
        "reconnectInterval": 5000
      }
    ]
  }
}
```

> NapCat 是 WebSocket **客户端**，Bot 是**服务端**。Bot 侧监听地址和端口由 `.env` 的 `HOST` / `PORT` 决定（默认 127.0.0.1:8080）。`url` 末尾的 `/onebot/v11/ws` 路径不能省。若 NapCat 与 Bot 不在同一台机器，把 `127.0.0.1` 换成 Bot 机器的 IP，并确认端口互通。

## 7. 启动 Bot

```bash
python bot.py
```

看到 NapCat 日志出现 `Websocket connected` / OneBot 事件即链路打通。机器人私聊回复 `/help` 可查看全部命令。

## 8. 验收测试清单

| 场景 | 操作 | 预期 |
|---|---|---|
| 私聊 AI | 私聊发送 `你好，你是谁` | LLM 回复 |
| 群 @ AI | 群里发送 `@机器人 解释一下 Dijkstra` | LLM 回复 |
| /ai 命令 | 群里发送 `/ai 写个俳句`（私聊同样支持） | LLM 回复 |
| 群普通消息不响应 | 群里发 `今天晚上吃什么` | 无反应 |
| 续聊 | 回复机器人上一条消息继续提问 | 带上下文回复 |
| 帮助 | 发送 `/help` | 列出全部命令 |
| 未知命令 | 私聊发送 `/foobar` | 提示未知命令 + 帮助（不调用 LLM） |
| 清空会话 | 发送 `/clear` | "当前会话已清空。" |
| 网页总结 | `/总结 https://example.com` | 先回"正在读取"，再按固定格式返回摘要 |
| @+URL 自动总结 | `@机器人 这篇文章主要说什么 https://example.com/article` | 自动进入总结 |
| SSRF 拒绝 | `/总结 http://127.0.0.1:8080` | "该地址不被允许访问" |
| 运行状态 | `/status` | 模型、会话数等（不含密钥） |
| 主动发送（管理员私聊机器人） | `/broadcast user:123456 -- 你好` | 预览 → `/confirm` → 发送并汇报统计 |
| 权限拒绝 | 非管理员发 `/broadcast ...` | "该命令仅管理员可用。" |
| 重复 /broadcast | 管理员连续两次 /broadcast | 只保留最新一条待确认，旧的自动作废 |
| GitHub 监控 | `/github add https://github.com/owner/repo` | 添加仓库后可手动检查或接收变化通知 |
| 每日日报 | `/report` | 查看当天 GitHub、任务和重要记忆汇总 |

主动发送也可以不经聊天窗口，用命令行（需 NapCat 开启 HTTP 服务端，环境变量 `ONEBOT_HTTP_URL` 指向其地址）：

```bash
python scripts/send_message.py user:123456 "你好"
python scripts/send_message.py group:789012 "通知"
```

## 9. 管理员配置

`.env` 中逗号分隔填写：

```
ADMIN_QQ_IDS=111111,222222
```

修改后重启 Bot。管理员专属能力：`/broadcast`、`/confirm`、`/cancel`。

## 10. 安全设计说明

- **SSRF**：所有抓取先用 `ipaddress` 做 scheme + DNS 解析后 IP 校验（loopback / RFC1918 / link-local（含 169.254.169.254 云 metadata）/ 组播 / 保留段全部拒绝），重定向每一跳重新校验，最多 5 跳，只允许 http/https，响应体有大小上限。已知限制：校验与请求之间存在 DNS rebinding 的理论窗口，生产环境可加自建解析 pin。
- **Prompt Injection**：网页内容永远包在 `<untrusted_web_content>` 定界符内送入 LLM，system prompt 明确声明其中任何指令无效；总结链路代码中不存在 `MessageDispatcher` 引用，网页内容在架构上不可能触发 QQ 发送。
- **发送权限**：`MessageDispatcher` 是唯一发送出口；主动发送只能来自管理员命令或用户已开启的定时通知任务，LLM 无工具调用能力。
- **群发**：管理员专属 + 数量上限（默认 20）+ 限速（默认 1 条/秒）+ 预览确认（TTL 5 分钟、发起人本人可确认、仅可执行一次、重复下发自动作废旧任务）。
- **敏感数据**：`.env` 已在 `.gitignore`；日志不输出 API Key，网页正文与发送内容截断后入库。
- **优雅关闭**：Bot 停机时自动释放 LLM/抓取 HTTP 客户端与数据库连接。
- **GitHub 监控**：使用 `GITHUB_CHECK_CRON` 周期检查；仅用户开启 `/notify github on` 后推送，GitHub Token 不写日志。
- **日报**：使用 `DAILY_REPORT_CRON` 触发；仅用户开启 `/notify report on` 后发送，内容写入 `reports` 表。

## 11. 常见报错

| 现象 | 原因 / 处理 |
|---|---|
| Bot 启动但 NapCat 一直重连失败 | 检查 `url` 路径是否为 `/onebot/v11/ws`；双方 `token` 是否一致；`HOST/PORT` 是否被占用 |
| 报 `401` / 鉴权失败 | token 不一致 |
| 收不到群消息 / 无法 @ 响应 | NapCat 未上报群消息，检查 NapCat 日志；确认机器人未被禁言 |
| AI 回复"服务暂时不可用" | 看 Bot 控制台 traceback：一般是 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` 填错或欠费 |
| 网页总结失败 | 目标站反爬（可开 `ENABLE_PLAYWRIGHT=true` 并 `pip install ".[playwright]"` + `playwright install chromium`，Bot 会自动降级用 Playwright 重试） |
| `pip install trafilatura` 失败 | 先升级 pip：`pip install -U pip`，确保有预编译 lxml wheel |

## 12. Windows 部署

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env   # 编辑后
.venv\Scripts\python bot.py
```

建议用 NSSM 或 任务计划程序 注册为常驻服务。

## 13. Linux 部署

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # 编辑后
.venv/bin/python bot.py
```

systemd 单元示例 `/etc/systemd/system/qq-llm-bot.service`：

```ini
[Unit]
After=network-online.target

[Service]
WorkingDirectory=/opt/qq-llm-bot
ExecStart=/opt/qq-llm-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 14. 开发

```bash
pip install -e ".[dev]"
pytest        # 单测：URL / SSRF / 权限 / 解析器 / 会话 / 记忆 / Scheduler / GitHub / 分块 / LLM / 缓存 / Dispatcher / 插件触发规则 / 注入防御
ruff check app tests
```

## 15. 后续路线

- `GROUP_SHARED_CONTEXT=true`（已支持）群共享上下文
- `ENABLE_PLAYWRIGHT=true`（已支持）JS 页面渲染兜底，抓取失败自动降级
- `WEB_CACHE_TTL_SECONDS`（已支持）URL 抓取缓存
- Agent Tool Calling（需要二次确认设计，本期刻意不做）
