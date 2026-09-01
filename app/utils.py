"""通用小工具。"""

QQ_TEXT_SAFE_LIMIT = 4000


def truncate_for_qq(text: str, limit: int = QQ_TEXT_SAFE_LIMIT) -> str:
    """超长回复截断，避免超出 QQ 单条消息上限导致发送失败。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n……（内容过长已截断，原文共 {len(text)} 字符）"
