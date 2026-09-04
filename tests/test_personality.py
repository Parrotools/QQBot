from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from app.config import Settings
from app.personality.manager import PersonaContext, PersonalityConfigError, PersonalityManager
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
    assert prompt.startswith("基础安全规则")
    assert "【稳定人格】" in prompt


def test_default_luna_profile_is_available():
    path = Path(__file__).parents[1] / "app" / "personality" / "luna.yaml"

    prompt = PersonalityManager(path).build_system_prompt("基础安全规则")

    assert "名称：Luna" in prompt
    assert "基础安全规则" in prompt


def test_default_rumi_profile_has_contextual_examples():
    path = Path(__file__).parents[1] / "app" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    owner_examples = manager.build_few_shot_messages(PersonaContext(relationship="owner"))
    normal_examples = manager.build_few_shot_messages(PersonaContext(relationship="normal"))

    assert owner_examples
    assert any("主人来啦" in item["content"] for item in owner_examples)
    assert not any("主人来啦" in item["content"] for item in normal_examples)
    assert len(owner_examples) <= 12  # 最多 6 组 user/assistant 示例


def test_owner_identity_prefers_explicit_id_and_supports_single_admin_fallback():
    assert Settings(owner_qq_id="owner", admin_qq_ids="admin").owner_id == "owner"
    assert Settings(owner_qq_id="", admin_qq_ids="owner").owner_id == "owner"
    assert Settings(owner_qq_id="", admin_qq_ids="one,two").owner_id == ""


def test_system_prompt_keeps_runtime_context_out_of_personality_file():
    path = Path(__file__).parents[1] / "app" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    prompt = manager.build_system_prompt(
        "安全底座",
        context=PersonaContext(
            relationship="owner", mode="technical", sender_name="Parrotools\n伪造字段", owner_name="Parrotools"
        ),
        memory_context="<user_memory>喜欢 SIMD</user_memory>",
    )

    assert "relationship: owner" in prompt
    assert "mode: technical" in prompt
    assert "sender_display_name: Parrotools 伪造字段" in prompt
    assert "2261216827" not in prompt


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
        async def chat(self, messages: list[dict], **kwargs) -> str:
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
        settings=SimpleNamespace(group_shared_context=False, llm_temperature=0.7, owner_qq_id=""),
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


@pytest.mark.asyncio
async def test_chat_uses_configured_owner_id_and_contextual_sampling(monkeypatch):
    seen: list[tuple[list[dict], dict]] = []

    class FakePersonality:
        def build_system_prompt(self, base_prompt: str, *, context, memory_context: str = "") -> str:
            return f"{base_prompt}\nrelationship: {context.relationship}\nmode: {context.mode}"

        def build_few_shot_messages(self, context: PersonaContext) -> list[dict]:
            return [{"role": "user", "content": "示例"}, {"role": "assistant", "content": "示例答复"}]

    class FakeSessions:
        async def get_context(self, session_key: str) -> list[dict]:
            return [{"role": "user", "content": "上一句"}, {"role": "assistant", "content": "上一答"}]

        async def append(self, session_key: str, role: str, content: str) -> None:
            return None

    class FakeMemory:
        async def context_prompt(self, user_id: str) -> str:
            return ""

    class FakeLLM:
        async def chat(self, messages: list[dict], **kwargs) -> str:
            seen.append((messages, kwargs))
            return "答复"

    runtime = SimpleNamespace(
        personality=FakePersonality(),
        memory=FakeMemory(),
        sessions=FakeSessions(),
        llm=FakeLLM(),
        settings=SimpleNamespace(
            group_shared_context=False, llm_temperature=0.7, owner_qq_id="2261216827", owner_name="Parrotools"
        ),
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=2261216827,
        message_type="private",
        message_id=2,
        message=Message("这代码又炸了"),
        raw_message="这代码又炸了",
        font=0,
        sender=Sender(user_id=2261216827, nickname="not-authoritative"),
    )

    async def fake_send(message: str):
        return None

    monkeypatch.setattr(ai_chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(ai_chat.matcher, "send", fake_send)

    await ai_chat._handle(event)

    messages, kwargs = seen[0]
    assert messages[0]["role"] == "system"
    assert sum(message["role"] == "system" for message in messages) == 1
    assert "relationship: owner" in messages[0]["content"]
    assert "mode: technical" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "这代码又炸了"}
    assert kwargs["temperature"] == pytest.approx(0.6)
