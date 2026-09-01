from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from app.personality.manager import PersonalityConfigError, PersonalityManager
from app.plugins import ai_chat


def test_loads_yaml_personality_and_builds_system_prompt(tmp_path: Path):
    path = tmp_path / "luna.yaml"
    path.write_text(
        """name: Luna
description: 一个理性、温和的 AI 助手。
tone:
  - 简洁
  - 有耐心
style:
  - 结构化回答
rules:
  - 不编造事实
""",
        encoding="utf-8",
    )

    prompt = PersonalityManager(path).build_system_prompt("基础安全规则")

    assert "名称：Luna" in prompt
    assert "语气：简洁；有耐心" in prompt
    assert "风格：结构化回答" in prompt
    assert "规则：不编造事实" in prompt
    assert prompt.endswith("基础安全规则")


def test_default_luna_profile_is_available():
    path = Path(__file__).parents[1] / "app" / "personality" / "luna.yaml"

    prompt = PersonalityManager(path).build_system_prompt("基础安全规则")

    assert "名称：Luna" in prompt
    assert "基础安全规则" in prompt


def test_personality_requires_name(tmp_path: Path):
    path = tmp_path / "invalid.yaml"
    path.write_text("description: missing name\n", encoding="utf-8")

    with pytest.raises(PersonalityConfigError, match="name"):
        PersonalityManager(path)


@pytest.mark.asyncio
async def test_chat_uses_personality_in_system_prompt(monkeypatch):
    seen: list[list[dict]] = []
    sent: list[str] = []

    class FakePersonality:
        def build_system_prompt(self, base_prompt: str) -> str:
            return "PERSONALITY\n" + base_prompt

    class FakeSessions:
        async def get_context(self, session_key: str) -> list[dict]:
            return []

        async def append(self, session_key: str, role: str, content: str) -> None:
            return None

    class FakeLLM:
        async def chat(self, messages: list[dict]) -> str:
            seen.append(messages)
            return "答复"

    class FakeMemory:
        async def context_prompt(self, user_id: str) -> str:
            return ""

    runtime = SimpleNamespace(
        personality=FakePersonality(),
        memory=FakeMemory(),
        sessions=FakeSessions(),
        llm=FakeLLM(),
        settings=SimpleNamespace(group_shared_context=False),
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=20000,
        message_type="private",
        message_id=1,
        message=Message("你好"),
        raw_message="你好",
        font=0,
        sender=Sender(user_id=20000, nickname="u"),
    )

    async def fake_send(message: str):
        sent.append(message)

    monkeypatch.setattr(ai_chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(ai_chat.matcher, "send", fake_send)

    await ai_chat._handle(event)

    assert seen[0][0]["content"].startswith("PERSONALITY\n")
    assert sent == ["答复"]
