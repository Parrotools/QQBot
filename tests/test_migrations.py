import aiosqlite

from app.database.db import Database
from app.database.models import SCHEMA


async def test_connect_adopts_legacy_database_without_losing_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.execute(
            "INSERT INTO memories (user_id, type, content, importance) VALUES (?, ?, ?, ?)",
            ("user-1", "fact", "保留的数据", 0.9),
        )
        await legacy.commit()
    finally:
        await legacy.close()

    upgraded = Database(path)
    await upgraded.connect()
    try:
        assert (await upgraded.fetch_memories("user-1"))[0]["content"] == "保留的数据"
        assert await upgraded.migration_versions() == [1, 2, 3]
    finally:
        await upgraded.close()
