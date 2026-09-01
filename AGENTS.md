# QQ LLM Bot 协作入口

- 定位：NapCatQQ + OneBot 11 + NoneBot2 的个人 QQ AI Bot，提供 LLM 对话、人格、记忆、网页总结、GitHub 监控、提醒和日报。
- 启动：复制 `.env.example` 为 `.env`，填写 OneBot token、LLM API Key；运行 `python bot.py`。
- 验证：运行 `pytest` 和 `ruff check app tests`；真实 QQ/NapCat 链路只能手动验收。
- 技术栈：Python 3.11+、asyncio、NoneBot2、httpx、aiosqlite、SQLite、APScheduler。
- 目录：`app/plugins` 负责事件入口，`app/services` 负责业务，`app/database` 负责 SQL，`app/security` 负责权限/SSRF/注入防护。
- 约定：业务层 ID 使用 `str`；数据库访问集中在 `Database`；所有主动 QQ 发送经过 `MessageDispatcher`；密钥只来自环境变量，不写日志。
- 当前状态（2026-09-01）：Personality、Memory、Scheduler、GitHub Tracker、日报和对应测试已实现；当前工作区改动尚未提交。
- 下一步：完成真实 QQ/NapCat 手动验收后，再决定是否扩展周报或 Agent Tool Calling。
