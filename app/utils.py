"""通用小工具。"""

import asyncio
from typing import Any

QQ_TEXT_SAFE_LIMIT = 4000


def truncate_for_qq(text: str, limit: int = QQ_TEXT_SAFE_LIMIT) -> str:
    """超长回复截断，避免超出 QQ 单条消息上限导致发送失败。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n……（内容过长已截断，原文共 {len(text)} 字符）"


async def send_local_reply(matcher: Any, runtime: Any, message: str) -> None:
    """发送不经过 LLM 的交互回复，并统一模拟处理延迟。"""
    try:
        delay = max(0.0, float(getattr(runtime.settings, "non_llm_reply_delay_seconds", 2.0)))
    except (TypeError, ValueError):
        delay = 2.0
    if delay:
        await asyncio.sleep(delay)
    await matcher.send(message)
