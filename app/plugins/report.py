"""日报查询命令。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id
from app.services.report import ReportError
from app.services.runtime import get_runtime

_REPORT_COMMANDS = ("/report", "/日报")


def parse_report_command(text: str) -> bool:
    value = text.strip().lower()
    return value in _REPORT_COMMANDS or any(value.startswith(f"{command} ") for command in _REPORT_COMMANDS)


async def _report_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if parse_report_command(event.message.extract_plain_text()):
        return claim_message_id(str(event.message_id))
    return False


report_matcher = on_message(rule=Rule(_report_rule), priority=6, block=True)


@report_matcher.handle()
async def _handle_report(event: MessageEvent):
    try:
        content = await get_runtime().report.build_daily_report(str(event.user_id))
    except ReportError as e:
        await report_matcher.send(f"日报生成失败：{e}")
        return
    await report_matcher.send(content)
