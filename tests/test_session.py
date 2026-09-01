import pytest

from app.database.db import Database
from app.services.session.manager import SessionManager


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


async def test_session_keys():
    assert SessionManager.private_key("123") == "private:123"
    assert SessionManager.group_key("g1", "u1") == "group:g1:u1"
    assert SessionManager.group_key("g1", "u1", shared=True) == "group:g1"
    assert SessionManager.group_key("g1", "u2") != SessionManager.group_key("g1", "u1")


async def test_context_order_and_content(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("1")
    await sm.append(key, "user", "你好")
    await sm.append(key, "assistant", "你好！")
    ctx = await sm.get_context(key)
    assert ctx == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]


async def test_trimming_to_max(db):
    sm = SessionManager(db, max_context_messages=4)
    key = SessionManager.private_key("2")
    for i in range(6):
        await sm.append(key, "user", f"m{i}")
    ctx = await sm.get_context(key)
    assert [c["content"] for c in ctx] == ["m2", "m3", "m4", "m5"]


async def test_independent_sessions(db):
    sm = SessionManager(db, max_context_messages=10)
    await sm.append(SessionManager.private_key("1"), "user", "a")
    await sm.append(SessionManager.private_key("2"), "user", "b")
    assert (await sm.get_context(SessionManager.private_key("1")))[0]["content"] == "a"
    assert (await sm.get_context(SessionManager.private_key("2")))[0]["content"] == "b"


async def test_clear(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("3")
    await sm.append(key, "user", "x")
    await sm.clear(key)
    assert await sm.get_context(key) == []
