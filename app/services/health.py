"""运行依赖健康检查与安全状态汇总。"""

import asyncio
import time
from typing import Any

from app.database.db import Database


class HealthService:
    def __init__(self, db: Database, llm: Any, github: Any, scheduler: Any, started_at: float, timeout: float = 3.0):
        self._db = db
        self._llm = llm
        self._github = github
        self._scheduler = scheduler
        self._started_at = started_at
        self._timeout = max(0.1, timeout)

    async def check(self) -> dict:
        database, llm, github = await asyncio.gather(
            self._probe(self._database_probe), self._probe(self._llm.check_health), self._probe(self._github.check_health)
        )
        counts = await self._counts() if database["ok"] else {}
        return {
            "ok": all(item["ok"] for item in (database, llm, github)),
            "uptime_seconds": int(time.monotonic() - self._started_at),
            "database": database,
            "llm": llm,
            "github": github,
            "scheduler": {"running": self._scheduler.is_running, "jobs": self._scheduler.job_count},
            "counts": counts,
            "recent_errors": await self._db.fetch_recent_errors() if database["ok"] else [],
        }

    async def _database_probe(self) -> None:
        await self._db.fetchone("SELECT 1 AS ok", ())

    async def _probe(self, operation) -> dict:
        try:
            await asyncio.wait_for(operation(), timeout=self._timeout)
        except TimeoutError:
            return {"ok": False, "detail": "Timeout"}
        except Exception as e:  # noqa: BLE001 — 健康检查必须隔离任意依赖失败
            return {"ok": False, "detail": type(e).__name__}
        return {"ok": True}

    async def _counts(self) -> dict[str, int | dict[str, int]]:
        github = await self._db.fetchone("SELECT COUNT(*) AS count FROM github_repositories", ())
        memories = await self._db.fetchone("SELECT COUNT(*) AS count FROM memories", ())
        return {
            "github_repositories": int(github["count"]),
            "memories": int(memories["count"]),
            "outbound_messages": await self._db.outbound_message_counts(),
        }
