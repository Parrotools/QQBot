import pytest

from app.database.db import Database
from app.services.qq.dispatcher import MessageDispatcher


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "queue.db"))
    await database.connect()
    yield database
    await database.close()


async def test_outbound_queue_delivers_a_scheduled_message(db):
    async def api_caller(action: str, **params) -> dict:
        assert action == "send_private_msg"
        assert params == {"user_id": 123, "message": "提醒内容"}
        return {"message_id": 9}

    dispatcher = MessageDispatcher(db, api_caller=api_caller)
    message_id = await dispatcher.enqueue_user("123", "提醒内容")

    assert await dispatcher.process_outbound_messages() == 1
    row = await db.fetchone("SELECT status, attempts, message_id FROM outbound_messages WHERE id = ?", (message_id,))
    assert row == {"status": "sent", "attempts": 1, "message_id": "9"}


async def test_outbound_queue_retries_a_failed_message(db):
    attempts = 0

    async def api_caller(action: str, **params) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return {"message_id": 10}

    dispatcher = MessageDispatcher(db, api_caller=api_caller, retry_delay_seconds=0)
    message_id = await dispatcher.enqueue_user("123", "稍后成功", max_attempts=3)

    await dispatcher.process_outbound_messages(limit=1)
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "retry",
        "attempts": 1,
    }
    await dispatcher.process_outbound_messages(limit=1)
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "sent",
        "attempts": 2,
    }


async def test_outbound_queue_marks_message_failed_after_max_attempts(db):
    async def api_caller(action: str, **params) -> dict:
        raise RuntimeError("permanent failure")

    dispatcher = MessageDispatcher(db, api_caller=api_caller, retry_delay_seconds=0)
    message_id = await dispatcher.enqueue_user("123", "最终失败", max_attempts=2)

    await dispatcher.process_outbound_messages()
    await dispatcher.process_outbound_messages()

    assert (await db.fetchone("SELECT status, attempts, last_error FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "failed",
        "attempts": 2,
        "last_error": "RuntimeError: permanent failure",
    }


async def test_outbound_queue_reclaims_expired_sending_lease(db):
    async def api_caller(action: str, **params) -> dict:
        return {"message_id": 11}

    dispatcher = MessageDispatcher(db, api_caller=api_caller)
    message_id = await dispatcher.enqueue_user("123", "恢复发送")
    claimed = await db.claim_outbound_message("9999-01-01 00:00:00", "2000-01-01 00:00:00")
    assert claimed["id"] == message_id
    await db.execute("UPDATE outbound_messages SET locked_at = ? WHERE id = ?", ("2000-01-01 00:00:00", message_id))

    assert await dispatcher.process_outbound_messages() == 1
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "sent",
        "attempts": 2,
    }
