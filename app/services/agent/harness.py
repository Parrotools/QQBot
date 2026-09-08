"""自然语言到受控业务工具的桥接层。

LLM 只负责选择一个白名单工具并填充参数；权限、参数校验、确认和真正的业务调用
全部由 Python 代码负责。这里暂时注册 GitHub 工具，后续新增工具只需加入注册表。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.promptlib import load_prompt
from app.services.github.client import GitHubAPIError
from app.services.github.tracker import GitHubTrackerError, format_check_result, parse_repo_url

logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = load_prompt("agent.txt")
_GITHUB_REPO_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AGENT_HINTS = (
    "github", "仓库", "repo", "项目", "添加", "加入", "移除", "删除", "列表", "list", "监控", "检查",
)
_MAX_REQUEST_CHARS = 4000

Handler = Callable[[Any, str, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    handler: Handler

    def prompt_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
        }


@dataclass(frozen=True)
class AgentResult:
    """工具层是否接管了本条消息，以及需要发送给用户的内容。"""

    handled: bool
    message: str


class AgentHarness:
    def __init__(self, *, confirmation_ttl_seconds: int = 300):
        self._confirmation_ttl_seconds = max(30, int(confirmation_ttl_seconds))
        self._tools = self._build_tools()

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def looks_like_tool_request(self, text: str) -> bool:
        value = str(text).strip().lower()
        return bool(value) and any(hint in value for hint in _AGENT_HINTS)

    async def try_handle(
        self,
        *,
        runtime: Any,
        event: Any,
        text: str,
        history: list[dict] | None = None,
    ) -> AgentResult | None:
        if not self.looks_like_tool_request(text):
            return None

        plan = await self._plan(runtime, text, history or [])
        if plan is None or plan["action"] == "none":
            return None

        tool = self._tools.get(plan["action"])
        if tool is None:
            return None
        arguments = self._normalize_arguments(tool.name, plan["arguments"])
        if arguments is None:
            return AgentResult(True, "我需要更明确的信息才能执行这个操作，例如完整的 GitHub 仓库链接。")

        user_id = str(event.user_id)
        if tool.read_only:
            try:
                return AgentResult(True, await tool.handler(runtime, user_id, arguments))
            except (GitHubAPIError, GitHubTrackerError, ValueError) as exc:
                return AgentResult(True, f"操作失败：{exc}")

        session_key = _session_key(event, runtime)
        summary = self._summary(tool.name, arguments)
        expires_at = _utc_now() + timedelta(seconds=self._confirmation_ttl_seconds)
        pending_id = await runtime.db.create_pending_agent_action(
            user_id,
            session_key,
            tool.name,
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            summary,
            expires_at.isoformat(),
        )
        logger.info("agent action pending_id=%s user=%s tool=%s", pending_id, user_id, tool.name)
        minutes = max(1, (self._confirmation_ttl_seconds + 59) // 60)
        return AgentResult(
            True,
            f"我理解为：{summary}\n请在 {minutes} 分钟内回复“确认执行”来执行，或回复“取消执行”。",
        )

    async def has_pending(self, *, runtime: Any, event: Any) -> bool:
        return (
            await runtime.db.fetch_pending_agent_action(
                str(event.user_id), _session_key(event, runtime), _utc_now().isoformat()
            )
        ) is not None

    async def confirm(self, *, runtime: Any, event: Any) -> str:
        user_id = str(event.user_id)
        session_key = _session_key(event, runtime)
        pending = await runtime.db.claim_pending_agent_action(user_id, session_key, _utc_now().isoformat())
        if pending is None:
            return "没有待确认的操作（可能已过期、已执行或不存在）。"

        tool = self._tools.get(str(pending["tool_name"]))
        if tool is None:
            await runtime.db.finish_pending_agent_action(pending["id"], "failed")
            return "待确认操作对应的工具已不可用，已取消。"
        try:
            arguments = json.loads(pending["arguments"])
            if not isinstance(arguments, dict):
                raise TypeError("操作参数格式无效")
            result = await tool.handler(runtime, user_id, arguments)
        except (GitHubAPIError, GitHubTrackerError, TypeError, ValueError, json.JSONDecodeError) as exc:
            await runtime.db.finish_pending_agent_action(pending["id"], "failed")
            logger.warning("agent action failed id=%s tool=%s error=%s", pending["id"], tool.name, exc)
            return f"执行失败：{exc}"
        await runtime.db.finish_pending_agent_action(pending["id"], "confirmed")
        logger.info("agent action confirmed id=%s user=%s tool=%s", pending["id"], user_id, tool.name)
        return result

    async def cancel(self, *, runtime: Any, event: Any) -> str:
        pending = await runtime.db.fetch_pending_agent_action(
            str(event.user_id), _session_key(event, runtime), _utc_now().isoformat()
        )
        if pending is None:
            return "没有待取消的操作。"
        await runtime.db.finish_pending_agent_action(pending["id"], "cancelled")
        return "已取消。"

    async def _plan(self, runtime: Any, text: str, history: list[dict]) -> dict[str, Any] | None:
        history_text = "\n".join(
            f"{item.get('role', 'unknown')}: {str(item.get('content', ''))[:1000]}" for item in history[-6:]
        )
        tool_text = json.dumps(
            [tool.prompt_schema() for tool in self._tools.values()], ensure_ascii=False, separators=(",", ":")
        )
        messages = [
            {"role": "system", "content": f"{AGENT_SYSTEM_PROMPT}\n<tools>{tool_text}</tools>"},
            {
                "role": "user",
                "content": (
                    f"<history>\n{history_text}\n</history>\n"
                    f"<current_request>\n{str(text).strip()[:_MAX_REQUEST_CHARS]}\n</current_request>"
                ),
            },
        ]
        try:
            raw = await runtime.llm.chat(messages, temperature=0.0, max_tokens=300)
        except Exception as exc:  # noqa: BLE001 —— 工具解析失败应回退为普通聊天
            logger.warning("agent plan failed error=%s", type(exc).__name__)
            return None
        return _parse_plan(raw, self.tool_names)

    def _build_tools(self) -> dict[str, AgentTool]:
        return {
            "github_add_repository": AgentTool(
                "github_add_repository",
                "将一个 GitHub 仓库加入当前用户的监控列表。",
                _repo_parameters(),
                False,
                _github_add,
            ),
            "github_remove_repository": AgentTool(
                "github_remove_repository",
                "从当前用户的 GitHub 监控列表移除一个仓库。",
                _repo_parameters(),
                False,
                _github_remove,
            ),
            "github_list_repositories": AgentTool(
                "github_list_repositories",
                "查看当前用户已经加入的 GitHub 监控仓库。",
                {"type": "object", "properties": {}, "additionalProperties": False},
                True,
                _github_list,
            ),
            "github_check_repository": AgentTool(
                "github_check_repository",
                "检查当前用户已加入的 GitHub 仓库是否有新变化。",
                _repo_parameters(),
                True,
                _github_check,
            ),
            "github_info_repository": AgentTool(
                "github_info_repository",
                "查询一个 GitHub 仓库的公开信息。",
                _repo_parameters(),
                True,
                _github_info,
            ),
        }

    @staticmethod
    def _normalize_arguments(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, str] | None:
        if not isinstance(arguments, Mapping):
            return None
        if tool_name == "github_list_repositories":
            return {} if not arguments else None
        if set(arguments) != {"url"}:
            return None
        value = str(arguments.get("url", "")).strip()
        if _GITHUB_REPO_SHORTHAND.fullmatch(value):
            value = f"https://github.com/{value}"
        try:
            return {"url": parse_repo_url(value).url}
        except GitHubTrackerError:
            return None

    @staticmethod
    def _summary(tool_name: str, arguments: dict[str, str]) -> str:
        labels = {
            "github_add_repository": "把 {url} 加入你的 GitHub 监控列表",
            "github_remove_repository": "从你的 GitHub 监控列表移除 {url}",
        }
        return labels.get(tool_name, f"执行 {tool_name}（{arguments}）").format(**arguments)


def _repo_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "完整的 GitHub 仓库 URL 或 owner/repo"}},
        "required": ["url"],
        "additionalProperties": False,
    }


def _session_key(event: Any, runtime: Any) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        shared = bool(getattr(runtime.settings, "group_shared_context", False))
        return f"group:{group_id}" if shared else f"group:{group_id}:{event.user_id}"
    return f"private:{event.user_id}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_plan(raw: str, tool_names: frozenset[str]) -> dict[str, Any] | None:
    value = str(raw).strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        plan = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(plan, dict):
        return None
    action = plan.get("action")
    arguments = plan.get("arguments", {})
    if not isinstance(action, str) or not isinstance(arguments, dict):
        return None
    if action != "none" and action not in tool_names:
        return None
    return {"action": action, "arguments": arguments}


async def _github_add(runtime: Any, user_id: str, arguments: dict[str, str]) -> str:
    repo = await runtime.github.add_repository(user_id, arguments["url"])
    return f'已添加仓库 {repo["repo_owner"]}/{repo["repo_name"]}。'


async def _github_remove(runtime: Any, user_id: str, arguments: dict[str, str]) -> str:
    if not await runtime.github.remove_repository(user_id, arguments["url"]):
        raise GitHubTrackerError("该仓库尚未添加")
    return "已移除仓库。"


async def _github_list(runtime: Any, user_id: str, arguments: dict[str, str]) -> str:
    del arguments
    repos = await runtime.github.list_repositories(user_id)
    if not repos:
        return "你还没有添加 GitHub 仓库。"
    return "你的 GitHub 仓库：\n" + "\n".join(
        f'{repo["repo_owner"]}/{repo["repo_name"]}' for repo in repos
    )


async def _github_check(runtime: Any, user_id: str, arguments: dict[str, str]) -> str:
    return format_check_result(await runtime.github.check(user_id, arguments["url"]))


async def _github_info(runtime: Any, user_id: str, arguments: dict[str, str]) -> str:
    del user_id
    ref = parse_repo_url(arguments["url"])
    info = await runtime.github_client.get_repository(ref.owner, ref.name)
    return (
        f"{ref.owner}/{ref.name}\n"
        f"Star：{info.get('stargazers_count', 0)}  Fork：{info.get('forks_count', 0)}\n"
        f"开放 Issue：{info.get('open_issues_count', 0)}\n{info.get('html_url', ref.url)}"
    )
