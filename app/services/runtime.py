"""运行时容器：所有服务的单例装配点。

bot.py 启动时调用 init_runtime()，关闭时调用 close_runtime()；
插件通过 get_runtime() 取用。
"""

import asyncio
import logging
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.database.db import Database
from app.security.permissions import PermissionService
from app.services.llm.base import LLMProvider
from app.services.llm.openai_compatible import OpenAICompatibleProvider
from app.services.qq.dispatcher import MessageDispatcher
from app.services.session.manager import SessionManager
from app.services.web.extractor import ExtractionError  # noqa: F401  便于插件统一导入
from app.services.web.fetcher import PlaywrightFetcher, WebFetcher
from app.services.web.summarizer import WebSummarizer


@dataclass
class Runtime:
    settings: Settings
    db: Database
    sessions: SessionManager
    llm: LLMProvider
    dispatcher: MessageDispatcher
    permission: PermissionService
    fetcher: WebFetcher
    playwright_fetcher: PlaywrightFetcher | None
    summarizer: WebSummarizer
    web_semaphore: asyncio.Semaphore


_runtime: Runtime | None = None


async def init_runtime() -> Runtime:
    global _runtime
    if _runtime is not None:
        return _runtime

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    db = Database(settings.database_path)
    await db.connect()

    llm = OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        max_concurrency=settings.max_concurrent_llm_tasks,
    )
    dispatcher = MessageDispatcher(db, rate_limit_per_second=settings.send_rate_limit_per_second)
    fetcher = WebFetcher(
        max_bytes=settings.max_webpage_bytes,
        connect_timeout=settings.web_fetch_timeout_connect,
        read_timeout=settings.web_fetch_timeout_read,
        cache_ttl=settings.web_cache_ttl_seconds,
    )
    playwright_fetcher = (
        PlaywrightFetcher(max_bytes=settings.max_webpage_bytes, navigation_timeout=settings.web_fetch_timeout_read)
        if settings.enable_playwright
        else None
    )
    summarizer = WebSummarizer(
        provider=llm,
        chunk_chars=settings.web_summary_chunk_chars,
        max_chunks=settings.web_summary_max_chunks,
    )

    _runtime = Runtime(
        settings=settings,
        db=db,
        sessions=SessionManager(db, settings.max_context_messages),
        llm=llm,
        dispatcher=dispatcher,
        permission=PermissionService(settings.admin_ids),
        fetcher=fetcher,
        playwright_fetcher=playwright_fetcher,
        summarizer=summarizer,
        web_semaphore=asyncio.Semaphore(max(1, settings.max_concurrent_web_tasks)),
    )
    return _runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Runtime 尚未初始化（bot 启动时由 init_runtime 创建）")
    return _runtime


async def close_runtime() -> None:
    """优雅关闭：释放 LLM HTTP 客户端、抓取客户端与数据库连接。"""
    global _runtime
    if _runtime is None:
        return
    rt, _runtime = _runtime, None
    logger = logging.getLogger(__name__)
    try:
        await rt.llm.aclose()
    except Exception:
        logger.exception("关闭 LLM 客户端失败")
    try:
        await rt.fetcher.aclose()
    except Exception:
        logger.exception("关闭抓取客户端失败")
    try:
        await rt.db.close()
    except Exception:
        logger.exception("关闭数据库失败")
