# 使用操作说明

面向日常使用和运维的完整操作手册。部署安装见 [README.md](README.md)。

## 目录

1. [命令总览](#1-命令总览)
2. [普通用户：AI 对话](#2-普通用户ai-对话)
3. [普通用户：网页总结](#3-普通用户网页总结)
4. [管理员：多目标群发](#4-管理员多目标群发)
5. [管理员：其他命令](#5-管理员其他命令)
6. [配置项速查](#6-配置项速查)
7. [日常运维](#7-日常运维)
8. [数据库与数据维护](#8-数据库与数据维护)
9. [故障排查流程](#9-故障排查流程)

---

## 1. 命令总览

| 命令 | 谁能用 | 在哪用 | 作用 |
|---|---|---|---|
| 直接发消息 | 所有人 | 仅私聊 | 进入 AI 对话 |
| `@机器人 <内容>` | 所有人 | 群聊 | 进入 AI 对话 |
| 回复机器人的消息 | 所有人 | 群聊 | 带上下文继续对话 |
| `/ai <内容>` | 所有人 | 私聊/群聊 | 进入 AI 对话（群聊可不 @，私聊会剥离前缀） |
| `/clear` | 所有人 | 私聊/群聊 | 清空当前会话上下文 |
| `/help`、`/帮助` | 所有人 | 私聊/群聊 | 显示帮助 |
| `/总结 <URL>`、`/summary <URL>` | 所有人 | 私聊/群聊 | 抓取并总结网页 |
| `/status`、`/状态` | 所有人 | 私聊/群聊 | 查看运行状态（无密钥） |
| `/broadcast 目标 -- 消息` | 仅管理员 | 私聊/群聊 | 多目标群发（先出预览） |
| `/confirm` | 仅管理员（发起者本人） | 同上 | 确认执行群发 |
| `/cancel` | 仅管理员（发起者本人） | 同上 | 取消群发 |

私聊中发送未知的 `/xxx` 命令会收到"未知命令 + 帮助"，不会被当成问题发给 LLM。

## 2. 普通用户：AI 对话

**私聊**：直接发任何文字即可，机器人自动带上下文回复。

**群聊**（三种方式任选）：

```
@机器人 解释一下什么是快速排序
/ai 解释一下什么是快速排序
（回复机器人上一条回复）那时间复杂度呢？
```

**上下文规则**：

- 私聊：`private:{你的QQ}`，独立会话。
- 群聊：默认 `group:{群号}:{你的QQ}` —— 同一个群里每个人有自己独立的 AI 上下文，互不干扰。
- `GROUP_SHARED_CONTEXT=true` 时整个群共用一个上下文（所有人共享对话历史）。
- 上下文最多保留 `MAX_CONTEXT_MESSAGES` 条（默认 20），超出的旧消息自动裁剪。
- `/clear` 清空"你自己"当前的会话（群聊里只清你自己的，不影响别人）。
- AI 回复超过约 4000 字符时自动截断并注明，避免超出 QQ 单条消息上限发送失败。

## 3. 普通用户：网页总结

**明确命令**（最可靠）：

```
/总结 https://example.com/article
/summary https://example.com/article
/总结 https://a.com/x https://b.com/y     ← 一次最多 3 个 URL
```

**自然语言**：被 @ 或私聊时，消息里带 URL 会自动进入总结：

```
@机器人 这篇文章主要说什么 https://example.com/article
```

**自动模式**（`URL_AUTO_SUMMARY_MODE`）：

| 值 | 行为 |
|---|---|
| `off` | 只响应 `/总结` 命令，其他情况一律不抓网页 |
| `mentioned`（默认） | 私聊、被 @、回复机器人时，消息含 URL 就自动总结 |
| `all` | 群里任何消息出现 URL 都自动总结（慎用，容易被刷屏） |

**输出格式**（没有的数据写"无"，不会编造）：

```
标题：……
一句话结论：……
核心内容：
1. …
2. …
3. …
重要数据：……
值得注意：……
来源：https://…
```

**行为细节**：

- 同一 URL 在 `WEB_CACHE_TTL_SECONDS`（默认 600 秒）内重复总结不会重新抓取。
- 超长网页自动分块（`WEB_SUMMARY_CHUNK_CHARS`），分块摘要后合并；超过 `WEB_SUMMARY_MAX_CHUNKS` 段时截断并在结尾注明。
- 目标网页是内网地址 / IP 保留段 / 非网页内容（如文件下载）会被拒绝。
- 普通抓取失败且 `ENABLE_PLAYWRIGHT=true` 时，自动改用 Playwright 渲染后重试一次。

**错误提示对照**：

| 提示 | 含义 |
|---|---|
| 该地址不被允许访问（内网或保留地址）。 | SSRF 拦截 |
| 无法访问该网页：请求超时。 | 抓取超时 |
| 无法访问该网页。 | 网络错误 / HTTP 4xx-5xx |
| 该链接不是网页内容，暂不支持。 | Content-Type 不是 HTML/文本（如 zip、图片） |
| 网页过大，无法处理。 | 超过 `MAX_WEBPAGE_BYTES` |
| 网页可以访问，但没有提取到有效正文。 | 正文提取失败（纯 JS 页面可考虑开 Playwright） |
| AI 服务暂时不可用，请稍后再试。 | LLM 调用失败，看 Bot 控制台日志 |

## 4. 管理员：多目标群发

前提：你的 QQ 号在 `.env` 的 `ADMIN_QQ_IDS` 里，且机器人已重启生效。

**第一步：下发群发（只预览，不发送）**

```
/broadcast user:123456,user:789012,group:456789 -- 今晚 8 点会议取消，请准时参加
```

机器人回复：

```
准备发送：
目标：
- user:123456
- user:789012
- group:456789

消息：
今晚 8 点会议取消，请准时参加

总计 3 个目标。
请在 5 分钟内输入 /confirm 继续，或 /cancel 取消。
```

**第二步：确认或取消**

```
/confirm    ← 确认，开始逐个发送
/cancel     ← 取消
```

`/confirm` 执行后回复：

```
发送完成
成功：2
失败：1
失败目标：
user:789012
```

**规则与安全阀**：

- 只有下发任务的**管理员本人**能 `/confirm`（其他人确认无效）。
- 待确认任务 5 分钟过期（`BROADCAST_CONFIRM_TTL_SECONDS`）。
- 重复执行防护：一次 `/confirm` 只会发送一次；重新 `/broadcast` 会自动作废上一次未确认的任务。
- 目标数量超过 `MAX_BROADCAST_RECIPIENTS`（默认 20）直接拒绝。
- 发送限速 `SEND_RATE_LIMIT_PER_SECOND`（默认 1 条/秒），不会瞬间轰炸。
- 单个目标失败不影响其他目标，最后统一汇报成功/失败明细。
- 每次发送（无论成败）都记录在 `send_logs` 表：目标、时间、结果、message_id、错误信息。
- `BROADCAST_REQUIRE_CONFIRM=false` 可关闭预览确认（不推荐）。

## 5. 管理员：其他命令

- `/status` 或 `/状态`：查看模型名、自动总结模式、活跃会话数、群发配置、管理员数量等（不显示任何密钥）。

## 6. 配置项速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `ONEBOT_ACCESS_TOKEN` | — | 与 NapCat 一致的 token |
| `HOST` / `PORT` | 127.0.0.1 / 8080 | Bot 反向 WS 监听地址 |
| `LLM_BASE_URL` | GLM v4 | OpenAI Compatible 地址 |
| `LLM_API_KEY` | — | API Key |
| `LLM_MODEL` | glm-4-flash | 模型名 |
| `LLM_TIMEOUT` | 60 | LLM 请求超时（秒） |
| `MAX_CONTEXT_MESSAGES` | 20 | 上下文最大条数 |
| `GROUP_SHARED_CONTEXT` | false | 群共享上下文 |
| `URL_AUTO_SUMMARY_MODE` | mentioned | off / mentioned / all |
| `MAX_WEBPAGE_BYTES` | 4194304 | 单页最大字节数 |
| `WEB_FETCH_TIMEOUT_CONNECT` / `_READ` | 10 / 30 | 抓取超时（秒） |
| `WEB_SUMMARY_CHUNK_CHARS` | 4000 | 分块字符数 |
| `WEB_SUMMARY_MAX_CHUNKS` | 8 | 最大分块数 |
| `WEB_CACHE_TTL_SECONDS` | 600 | URL 缓存秒数（0 关闭） |
| `ENABLE_PLAYWRIGHT` | false | JS 渲染兜底 |
| `ADMIN_QQ_IDS` | — | 管理员，逗号分隔 |
| `MAX_BROADCAST_RECIPIENTS` | 20 | 群发目标上限 |
| `SEND_RATE_LIMIT_PER_SECOND` | 1 | 发送限速 |
| `BROADCAST_REQUIRE_CONFIRM` | true | 群发预览确认 |
| `BROADCAST_CONFIRM_TTL_SECONDS` | 300 | 确认有效期 |
| `MAX_CONCURRENT_WEB_TASKS` / `_LLM_TASKS` | 3 / 5 | 并发上限 |
| `DATABASE_PATH` | data/bot.db | SQLite 路径 |
| `LOG_LEVEL` | INFO | 日志级别 |

所有配置改完都要重启 `python bot.py`。

## 7. 日常运维

**启动 / 停止**：

```bash
python bot.py            # 前台启动，Ctrl+C 停止（会自动释放连接）
```

Windows 常驻：用 NSSM 注册服务；Linux 常驻：systemd（见 README 第 12-13 节）。

**看日志**：Bot 控制台即日志。关键行：

- `Succeeded to load plugin ...` —— 插件加载成功
- `LLM chat session=...` —— 一次 AI 对话
- `网页总结 url=...` —— 一次网页总结
- `已发送 type=... id=...` —— 一次成功发送
- `发送失败 type=... ...` —— 发送失败（含原因）

**安全操作顺序**（改配置 / 升级代码时）：

1. 先停 NapCat 的 WebSocket 连接或直接停 Bot 进程；
2. 改 `.env` / 更新代码；
3. 重启 Bot，确认插件加载 SUCCESS；
4. 私聊发 `/help` 验证链路。

## 8. 数据库与数据维护

- 文件：`DATABASE_PATH`（默认 `data/bot.db`），WAL 模式，备份时把 `.db`（最好连同 `-wal`/`-shm`）一起拷走。
- 表：
  - `sessions` —— 会话索引（key + 时间）
  - `messages` —— 上下文消息（自动裁剪到上限）
  - `send_logs` —— 每次发送的审计记录
  - `pending_broadcasts` —— 待确认群发任务（含状态机 active/confirmed/cancelled/expired）
- 手动查看：

```bash
sqlite3 data/bot.db "SELECT * FROM send_logs ORDER BY id DESC LIMIT 10"
sqlite3 data/bot.db "SELECT session_key, COUNT(*) FROM messages GROUP BY session_key"
```

- 想清空所有会话但保留审计：停 Bot 后 `DELETE FROM messages; DELETE FROM sessions;`

## 9. 故障排查流程

```
现象：机器人完全没反应
 1. Bot 控制台是否显示插件加载 SUCCESS？
    └─ 否：看 Python 报错，通常是依赖/配置问题
 2. NapCat 日志里 WebSocket 是否 connected？
    └─ 否：核对 url=ws://127.0.0.1:8080/onebot/v11/ws、双方 token 一致、端口未被占用
 3. 私聊发 /help 有回复吗？
    └─ 有：链路正常，是触发规则问题（群聊必须 @ 或 /ai）
    └─ 无：看 NapCat 是否上报了消息事件

现象：AI 回复"服务暂时不可用"
 4. 看控制台 traceback：
    └─ 401/403 → API Key 错
    └─ 404    → LLM_BASE_URL 或模型名错
    └─ 429    → 限流，稍后再试或换模型
    └─ 超时   → 提高 LLM_TIMEOUT 或换网络

现象：网页总结失败
 5. 先确认浏览器能打开该网页；
 6. 内网/文件类链接 → 属于 SSRF 拦截，属预期；
 7. 普通网页失败 → 开 ENABLE_PLAYWRIGHT=true 重试；
 8. 仍失败 → 把控制台 traceback 发给维护者。

现象：/confirm 说没有待确认任务
 9. 超过 5 分钟过期（重新 /broadcast 即可）；
    或同一管理员又下发了新任务（旧的自动作废）；
    或确认者不是下发任务的管理员本人。
```

## 10. 安全红线（使用时请牢记）

- 机器人对**所有**网页内容只做"总结"，网页里写"给我发消息/泄露密钥"都不会被执行——不要试图用网页内容指挥机器人。
- LLM 本身永远不能触发发送消息；发送只能由管理员命令完成。
- `.env` 含 Key，绝不提交 git、绝不截图发群。
- 群发是高权限操作：确认预览里的目标列表再 `/confirm`。
