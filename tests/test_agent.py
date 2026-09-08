from types import SimpleNamespace

import pytest

from app.database.db import Database
from app.services.agent.harness import AgentHarness, _parse_plan


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "agent.db"))
    await database.connect()
    yield database
    await database.close()


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append(messages)
        return self.response


class FakeGitHub:
    def __init__(self):
        self.added: list[tuple[str, str]] = []

    async def add_repository(self, user_id: str, url: str) -> dict:
        self.added.append((user_id, url))
        return {"repo_owner": "OpenAI", "repo_name": "openai-python"}

    async def list_repositories(self, user_id: str) -> list[dict]:
        return [{"repo_owner": "OpenAI", "repo_name": "openai-python"}] if user_id == "user-1" else []


def _runtime(db, llm, github):
    return SimpleNamespace(
        db=db,
        llm=llm,
        github=github,
        github_client=SimpleNamespace(),
        settings=SimpleNamespace(group_shared_context=False),
    )


def _event(user_id: str = "user-1"):
    return SimpleNamespace(user_id=user_id, group_id=None)


def test_parse_plan_accepts_json_code_fence_and_rejects_unknown_tool():
    allowed = frozenset({"github_list_repositories"})

    assert _parse_plan('```json\n{"action":"github_list_repositories","arguments":{}}\n```', allowed) == {
        "action": "github_list_repositories",
        "arguments": {},
    }
    assert _parse_plan('{"action":"run_shell","arguments":{}}', allowed) is None
    assert _parse_plan("not json", allowed) is None


@pytest.mark.asyncio
async def test_mutating_tool_is_pending_until_confirmation(db):
    llm = FakeLLM('{"action":"github_add_repository","arguments":{"url":"OpenAI/openai-python"}}')
    github = FakeGitHub()
    runtime = _runtime(db, llm, github)
    harness = AgentHarness(confirmation_ttl_seconds=300)

    result = await harness.try_handle(runtime=runtime, event=_event(), text="把这个仓库加入我的 GitHub 列表")

    assert result is not None and result.handled
    assert "确认执行" in result.message
    assert github.added == []
    pending = await db.fetchone("SELECT tool_name, status, arguments FROM pending_agent_actions", ())
    assert pending["tool_name"] == "github_add_repository"
    assert pending["status"] == "pending"
    assert "https://github.com/OpenAI/openai-python" in pending["arguments"]

    confirmed = await harness.confirm(runtime=runtime, event=_event())
    assert confirmed == "已添加仓库 OpenAI/openai-python。"
    assert github.added == [("user-1", "https://github.com/OpenAI/openai-python")]
    assert await harness.confirm(runtime=runtime, event=_event()) == "没有待确认的操作（可能已过期、已执行或不存在）。"


@pytest.mark.asyncio
async def test_read_only_tool_runs_without_confirmation(db):
    llm = FakeLLM('{"action":"github_list_repositories","arguments":{}}')
    github = FakeGitHub()
    runtime = _runtime(db, llm, github)
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="看看我的 GitHub 仓库列表")

    assert result is not None and result.handled
    assert result.message == "你的 GitHub 仓库：\nOpenAI/openai-python"
    assert await db.fetchone("SELECT id FROM pending_agent_actions", ()) is None


@pytest.mark.asyncio
async def test_non_request_skips_planner(db):
    llm = FakeLLM('{"action":"none","arguments":{}}')
    runtime = _runtime(db, llm, FakeGitHub())
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="今天天气怎么样？")

    assert result is None
    assert llm.calls == []
