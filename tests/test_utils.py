from types import SimpleNamespace

import pytest

from app.utils import send_local_reply


@pytest.mark.asyncio
async def test_send_local_reply_uses_configured_delay(monkeypatch):
    delays: list[float] = []
    sent: list[str] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeMatcher:
        async def send(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setattr("app.utils.asyncio.sleep", fake_sleep)

    runtime = SimpleNamespace(settings=SimpleNamespace(non_llm_reply_delay_seconds=1.5))
    await send_local_reply(FakeMatcher(), runtime, "本地回复")

    assert delays == [1.5]
    assert sent == ["本地回复"]


@pytest.mark.asyncio
async def test_send_local_reply_can_be_disabled(monkeypatch):
    delays: list[float] = []
    sent: list[str] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeMatcher:
        async def send(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setattr("app.utils.asyncio.sleep", fake_sleep)

    runtime = SimpleNamespace(settings=SimpleNamespace(non_llm_reply_delay_seconds=0))
    await send_local_reply(FakeMatcher(), runtime, "立即回复")

    assert delays == []
    assert sent == ["立即回复"]
