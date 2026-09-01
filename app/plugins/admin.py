"""管理员/通用插件：/status 查看运行状态（不含任何密钥）。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id
from app.services.runtime import get_runtime


async def _status_trigger(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if event.message.extract_plain_text().strip() in ("/status", "/状态"):
        return claim_message_id(str(event.message_id))
    return False


status_matcher = on_message(rule=Rule(_status_trigger), priority=5, block=True)


@status_matcher.handle()
async def _handle(event: MessageEvent, matcher_: Matcher):
    runtime = get_runtime()
    s = runtime.settings
    row = await runtime.db.fetchone("SELECT COUNT(*) AS c FROM sessions")
    session_count = row["c"] if row else 0
    await matcher_.send(
        "qq-llm-bot 运行状态：\n"
        f"模型：{s.llm_model}\n"
        f"网页自动总结：{s.url_auto_summary_mode}（缓存 {int(s.web_cache_ttl_seconds)} 秒）\n"
        f"群聊共享上下文：{s.group_shared_context}\n"
        f"上下文上限：{s.max_context_messages} 条，活跃会话：{session_count}\n"
        f"群发确认：{s.broadcast_require_confirm}（上限 {s.max_broadcast_recipients} 个目标）\n"
        f"管理员数量：{len(runtime.permission.admin_ids)}\n"
        f"Playwright：{s.enable_playwright}"
    )
