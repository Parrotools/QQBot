"""集中配置：全部来自环境变量 / .env，禁止硬编码任何密钥、QQ 号、地址。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OneBot / NoneBot
    onebot_access_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8080

    # LLM（OpenAI Compatible）
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"
    llm_timeout: float = 60.0

    # 会话
    max_context_messages: int = 20
    group_shared_context: bool = False

    # 网页
    url_auto_summary_mode: Literal["off", "mentioned", "all"] = "mentioned"
    max_webpage_bytes: int = 4_194_304
    web_fetch_timeout_connect: float = 10.0
    web_fetch_timeout_read: float = 30.0
    web_summary_chunk_chars: int = 4000
    web_summary_max_chunks: int = 8
    enable_playwright: bool = False
    # 同一 URL 抓取结果缓存秒数（0 关闭）
    web_cache_ttl_seconds: float = 600.0

    # 权限与群发
    admin_qq_ids: str = ""
    max_broadcast_recipients: int = 20
    send_rate_limit_per_second: float = 1.0
    broadcast_require_confirm: bool = True
    broadcast_confirm_ttl_seconds: int = 300

    # 并发
    max_concurrent_web_tasks: int = 3
    max_concurrent_llm_tasks: int = 5

    # 数据 / 日志
    database_path: str = "data/bot.db"
    log_level: str = "INFO"

    @property
    def admin_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.admin_qq_ids.split(",") if x.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
