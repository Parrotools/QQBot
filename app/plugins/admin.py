"""管理员/通用插件：/status 查看运行状态（不含任何密钥）。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id, command_is_addressed, strip_bot_mention
from app.services.runtime import get_runtime
from app.utils import send_local_reply


async def _status_trigger(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if strip_bot_mention(event.message.extract_plain_text()) in ("/status", "/状态"):
        return claim_message_id(str(event.message_id))
    return False


status_matcher = on_message(rule=Rule(_status_trigger), priority=5, block=True)


@status_matcher.handle()
async def _handle(event: MessageEvent, matcher_: Matcher):
    runtime = get_runtime()
    status = await runtime.health.check()
    counts = status["counts"]
    errors = status["recent_errors"]
    await send_local_reply(matcher_, runtime,
        "qq-llm-bot 运行状态：\n"
        f"运行时间：{status['uptime_seconds']} 秒\n"
        f"LLM：{'正常' if status['llm']['ok'] else '异常'}\n"
        f"数据库：{'正常' if status['database']['ok'] else '异常'}\n"
        f"Scheduler：{'运行中' if status['scheduler']['running'] else '未运行'}（{status['scheduler']['jobs']} 个任务）\n"
        f"GitHub 监控：{counts.get('github_repositories', 0)}\n"
        f"Memory：{counts.get('memories', 0)}\n"
        f"最近错误：{errors[0]['error'] if errors else '无'}"
    )
