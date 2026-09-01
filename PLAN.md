# qq-llm-bot 策划方案

> 目标：用一个真实 QQ 账号跑起来的 LLM 聊天机器人，支持会话上下文、网页抓取与总结、
> 主动/群发消息（带权限与确认机制），Windows / Linux 双平台可部署。

---

## 1. 总体架构

```
真实 QQ 账号
   │  (QQ 登录协议由 NapCat 实现，业务代码绝不碰协议)
   ▼
NapCatQQ  ──(OneBot 11 反向 WebSocket)──▶  NoneBot2 (Python 3.11+, asyncio)
                                              │
                    ┌─────────────────────────┼──────────────────────────┐
                    ▼                         ▼                          ▼
               plugins/ 事件路由          services/ 业务层             security/
            (ai_chat / web_summary     (LLM Gateway / Web 管道 /      (权限 / SSRF /
             / admin / broadcast)       Session / Dispatcher)         Prompt Injection)
                                              │
                                              ▼
                                        SQLite (aiosqlite)
```

关键点：

- **协议层与业务层彻底隔离**。NapCat 只做「QQ ↔ OneBot 11」转换；NoneBot2 用
  `nonebot-adapter-onebot`（v11）收事件。业务层只认 OneBot 事件模型。
- **反向 WebSocket**：由 NapCat 主动连 Bot。Bot 侧监听 `ws://127.0.0.1:8080/onebot/v11/ws`，
  双方配同一个 access token。这样 Bot 侧不需要公网 IP，NapCat 放在家/服务器都行。
- **LLM 走 OpenAI Compatible API**，`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 全部来自环境变量，
  不绑厂商（GLM / DeepSeek / Qwen / OpenAI / vLLM 均可）。

## 2. 技术选型与理由

| 项 | 选型 | 理由 |
|---|---|---|
| Python | 3.11+ | NoneBot2 与 asyncio 生态最佳版本区间 |
| 协议接入 | NapCatQQ | 活跃维护、Windows/Linux 都有发行版、OneBot 11 实现完整 |
| Bot 框架 | NoneBot2 + OneBot v11 adapter | 事件/权限/过滤器现成，避免手写 WS 协议 |
| LLM 调用 | httpx.AsyncClient 自实现 `OpenAICompatibleProvider` | 比引入 langchain 轻得多，接口可控、易 mock 测试 |
| 数据库 | SQLite + **aiosqlite**（薄 repository 封装） | 第一版只有 4 张表、单实例、低并发，SQLAlchemy async 属于过度设计；封装 repository 后未来可换 |
| 网页抓取 | httpx（异步、可限大小、可控 redirect） | 需要逐跳 SSRF 校验，必须手动控制 redirect 流程，aiohttp/httpx 均可，选 httpx |
| 正文提取 | trafilatura，失败降级 BeautifulSoup | trafilatura 正文质量最好；BS4 兜底保证可用性 |
| JS 渲染 | Playwright（预留，`ENABLE_PLAYWRIGHT=false` 默认关） | 重依赖，按需启用；同样过 SSRF 检查 |
| 包管理 | pyproject.toml + uv（推荐），兼容 `pip install .` | uv 快，但不强制 |
| 日志 | logging（标准库）+ 自定义 filter | 避免引入 loguru 依赖，格式统一且能强制脱敏 |

## 3. 核心设计决策

1. **ID 全部 str**：`user_id / group_id / message_id` 业务层一律 `str`，与 OneBot API 字符串化参数对齐。
2. **会话模型**：私聊 `private:{user_id}`；群聊 `group:{group_id}:{user_id}`（每人独立上下文），
   预留 `GROUP_SHARED_CONTEXT` 切换为 `group:{group_id}`。历史条数用 `MAX_CONTEXT_MESSAGES` 截断，
   SQLite 存 `sessions`、`messages` 两表。
3. **群聊触发面收窄**：群内只有 `@机器人` 或 `/ai` 才调 LLM；普通聊天绝不响应；过滤自身消息
   与重复 `message_id`（内存 LRU 去重即可，不必入库）。
4. **网页管道**：`URLParser → SSRFGate → Fetcher → Extractor → Chunker → Map-Reduce Summarizer`。
   - SSRF：`ipaddress` 库判断，覆盖 loopback / 私网 / link-local(含 169.254.169.254) / 组播 / 保留段；
     DNS 解析后校验 IP；**redirect 每一跳重新校验**并限制跳数；只允许 http/https。
   - Map-Reduce：按 `WEB_SUMMARY_CHUNK_CHARS` 切块、`WEB_SUMMARY_MAX_CHUNKS` 截断，
     分块摘要后合并出最终摘要，绝不整页塞给 LLM。
5. **Prompt Injection 原则**：网页内容一律是不可信数据。总结链路拿不到 `MessageDispatcher`，
   LLM 无任何工具调用能力，从架构上保证「网页让我发消息」不可能被执行。
6. **发送能力隔离**：`MessageDispatcher`（send_user / send_group / broadcast）只能被确定性的
   管理员命令调用；LLM 永远不能触发发送。每次发送写 `send_logs`（目标/时间/结果/message_id/错误）。
7. **群发安全阀**：仅 `ADMIN_QQ_IDS` 可用；`MAX_BROADCAST_RECIPIENTS`（默认 20）上限；
   `SEND_RATE_LIMIT_PER_SECOND=1` 逐条限速；`BROADCAST_REQUIRE_CONFIRM=true` 时先出预览，
   `/confirm` 在 TTL（5 分钟）内、且仅同一管理员可确认；`pending_broadcasts` 表持久化；
   单个目标失败不影响其余目标，最后汇报成功/失败明细。
8. **并发保护**：`MAX_CONCURRENT_WEB_TASKS` / `MAX_CONCURRENT_LLM_TASKS` 两个 Semaphore，
   慢网页/慢 LLM 不会拖死整个 Bot。
9. **错误面**：用户只看到简短中文错误（"AI 服务暂时不可用"），traceback 只进日志；
   日志强制过滤 API Key，网页正文截断后再入日志。

## 4. 目录结构

```
qq-llm-bot/
├── pyproject.toml / README.md / .env.example / .gitignore
├── bot.py                      # 入口：nonebot.init + 加载 app.plugins
├── app/
│   ├── config.py               # 环境变量 → Pydantic Settings
│   ├── plugins/                # ai_chat / web_summary / admin / broadcast
│   ├── services/
│   │   ├── llm/                # base.py(抽象) + openai_compatible.py
│   │   ├── web/                # url_parser / fetcher / extractor / summarizer
│   │   ├── qq/dispatcher.py    # MessageDispatcher（唯一发送出口）
│   │   └── session/manager.py  # 会话存取与截断
│   ├── database/               # db.py(aiosqlite 连接+建表) + models.py
│   ├── security/               # permissions / ssrf / prompt_injection(清洗+定界)
│   └── prompts/                # chat.txt / webpage_summary.txt
├── scripts/send_message.py     # 命令行调用 Dispatcher（走 Bot 实例或仅测试用）
├── tests/                      # URL / SSRF / 权限 / broadcast parser / session / chunking
└── data/                       # SQLite 文件（gitignore）
```

## 5. 分阶段实施（每阶段：import 检查 → 启动检查 → 跑测试 → 修复 → 下一阶段）

| Phase | 内容 | 验收 |
|---|---|---|
| 1 | 骨架 + config + NoneBot2/OneBot11 + Echo | QQ→NapCat→Bot→QQ 通 |
| 2 | LLM Gateway + 私聊 AI + 群 @ + /ai | 场景 1/2/3 |
| 3 | SQLite Session + 上下文 + /clear | 多轮上下文生效 |
| 4 | URLParser + SSRF + Fetcher + Extractor | 场景 10 拒绝 127.0.0.1 |
| 5 | Map-Reduce 总结 + /总结 + 自动模式 | 场景 4/5，注入文本只被总结（场景 6） |
| 6 | Dispatcher：send_user / send_group + send_logs | 主动发送成功 |
| 7 | broadcast + 权限 + confirm + 限速 + 日志 | 场景 7/8/9 |
| 8 | 测试补齐 + README（含 NapCat 部署、双端口填写说明） | 21 条 README 目录齐 |

## 6. 风险与注意

- **账号风控**：真实账号挂协议有被冻结风险，建议用小号；NapCat 保持更新。
- **NapCat 安装**：Windows 提供 NapCat.Shell（zip 免安装）/ NapCat.Installer；Linux 推荐
  Docker 或一键脚本。README 给出双方地址/端口/token 的明确填法，不留"配置 WebSocket 即可"这种模糊话。
- **trafilatura 在 Windows 依赖 lxml**，Python 3.11 有 wheel，正常 pip 可装。
- **测试策略**：SSRF/URL/parser/session/chunking 纯逻辑用 pytest 覆盖；端到端链路需要真实
  QQ 登录，作为手动验收清单而不是 CI 用例。
- **后续扩展位**：GROUP_SHARED_CONTEXT、PlaywrightFetcher、Agent Tool Calling（需二次确认设计）、
  多实例部署——本期一律只留接口不留实现。

## 7. 验收场景（10 条）

对应提示词中的场景 1–10：私聊 AI、群 @ AI、群普通消息不响应、/总结、@+URL 自动总结、
注入网页不可触发发送、普通用户禁 broadcast、管理员 broadcast 预览+confirm、部分失败继续并汇报、
SSRF 拒绝内网。全部通过即 MVP 完成。
