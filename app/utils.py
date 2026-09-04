"""通用小工具。"""

import asyncio
from typing import Any

QQ_TEXT_SAFE_LIMIT = 4000


def truncate_for_qq(text: str, limit: int = QQ_TEXT_SAFE_LIMIT) -> str:
    """超长回复截断，避免超出 QQ 单条消息上限导致发送失败。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n……（内容过长已截断，原文共 {len(text)} 字符）"


def local_reply_delay(runtime: Any, message: str) -> float:
    """按回复文本量计算不调用 LLM 的交互延迟。"""
    settings = runtime.settings
    try:
        legacy_max = getattr(settings, "non_llm_reply_delay_seconds", None)
        max_delay = float(
            legacy_max
            if legacy_max is not None
            else getattr(settings, "non_llm_reply_delay_max_seconds", 4.0)
        )
        if max_delay <= 0:
            return 0.0
        min_delay = max(0.0, min(float(getattr(settings, "non_llm_reply_delay_min_seconds", 0.5)), max_delay))
        chars_per_second = max(1.0, float(getattr(settings, "non_llm_reply_delay_chars_per_second", 35.0)))
    except (TypeError, ValueError):
        min_delay, max_delay, chars_per_second = 0.5, 4.0, 35.0

    visible_chars = len("".join(str(message).split()))
    return min(max_delay, max(min_delay, visible_chars / chars_per_second))


async def send_local_reply(matcher: Any, runtime: Any, message: str) -> None:
    """发送不经过 LLM 的交互回复，并按文本量模拟处理延迟。"""
    delay = local_reply_delay(runtime, message)
    if delay:
        await asyncio.sleep(delay)
    await matcher.send(message)
