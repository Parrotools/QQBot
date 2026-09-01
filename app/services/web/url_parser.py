"""URL 识别：从中文混排消息中提取 http/https URL，去重并保持顺序。"""

import re

URL_PATTERN = re.compile(r"https?://[^\s<>\"'）】\]]+", re.IGNORECASE)

# 中文/英文常见会吸附在 URL 后面的标点
_TRAILING_PUNCT = ".,;:!?。，、；：！？》〉\"'`"


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in URL_PATTERN.finditer(text or ""):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        # 未配对的右括号视为标点
        while u.endswith(")") and "(" not in u:
            u = u[:-1]
        if u and u.lower() not in seen:
            seen.add(u.lower())
            urls.append(u)
    return urls


def has_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text or ""))
