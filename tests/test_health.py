from types import SimpleNamespace

import pytest

from app.database.db import Database
from app.services.health import HealthService


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "health.db"))
    await database.connect()
    yield database
    await database.close()


class HealthyDependency:
    async def check_health(self):
        return None


class FailedDependency:
    async def check_health(self):
        raise RuntimeError("unavailable")


async def test_health_reports_dependencies_counts_and_recent_error(db):
    await db.insert_memory("user-1", "fact", "重要事项", 0.9)
    await db.insert_github_repository("user-1", "OpenAI", "openai-python", "https://github.com/OpenAI/openai-python")
    await db.insert_send_log("user", "123", "内容", False, None, "send failed")
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=2),
        started_at=0,
    )

    result = await health.check()

    assert result["database"]["ok"] is True
    assert result["llm"]["ok"] is True
    assert result["github"]["ok"] is True
    assert result["scheduler"] == {"running": True, "jobs": 2}
    assert result["counts"]["github_repositories"] == 1
    assert result["counts"]["memories"] == 1
    assert result["recent_errors"][0]["error"] == "send failed"


async def test_health_isolates_dependency_failure(db):
    health = HealthService(
        db,
        FailedDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=False, job_count=0),
        started_at=0,
    )

    result = await health.check()

    assert result["database"]["ok"] is True
    assert result["llm"] == {"ok": False, "detail": "RuntimeError"}
