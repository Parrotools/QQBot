"""自然语言到受控业务工具的桥接层。

LLM 只负责选择一个白名单工具并填充参数；权限、参数校验、确认和真正的业务调用
全部由 Python 代码负责。工具实现复用现有 service，不通过拼接命令字符串执行。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.promptlib import load_prompt
from app.services.github.client import GitHubAPIError
from app.services.github.tracker import GitHubTrackerError, format_check_result, parse_repo_url
from app.services.memory import VALID_MEMORY_TYPES, MemoryValidationError
from app.services.notifications import NotificationSettingsError
from app.services.qq.broadcast_parser import BroadcastFormatError, parse_targets
from app.services.report import ReportError
from app.services.scheduler import SchedulerValidationError, parse_reminder_command
from app.services.web.url_parser import extract_urls
from app.utils import truncate_for_qq

logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = load_prompt("agent.txt")
_GITHUB_REPO_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AGENT_HINTS = (
    "github", "仓库", "repo", "项目", "添加", "加入", "移除", "删除", "列表", "list", "监控", "检查",
    "记住", "记忆", "memory", "memories", "长期记忆", "提醒", "通知", "定时", "每天", "群发", "广播", "发送",
    "日报", "状态", "清空", "重置会话", "段子", "主题", "消息",
)
_MAX_REQUEST_CHARS = 4000
_MAX_AGENT_TEXT_CHARS = 2000

Handler = Callable[[Any, str, dict[str, Any], str], Awaitable[str]]


class AgentArgumentError(ValueError):
    """模型返回的工具参数不符合业务约束。"""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    handler: Handler
    requires_admin: bool = False

    def prompt_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
            "requires_admin": self.requires_admin,
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
        try:
            arguments = self._normalize_arguments(
                tool.name, plan["arguments"], runtime, history=history or [], request_text=text
            )
        except AgentArgumentError as exc:
            return AgentResult(True, f"参数不合法：{exc}")
        if arguments is None:
            return AgentResult(True, self._missing_arguments_message(tool.name))

        user_id = str(event.user_id)
        session_key = _session_key(event, runtime)
        if self._needs_admin(tool, arguments) and not runtime.permission.is_admin(user_id):
            return AgentResult(True, "该操作仅管理员可用。")

        if tool.read_only:
            try:
                return AgentResult(True, await tool.handler(runtime, user_id, arguments, session_key))
            except _SERVICE_ERRORS as exc:
                return AgentResult(True, f"操作失败：{exc}")

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
            if self._needs_admin(tool, arguments) and not runtime.permission.is_admin(user_id):
                raise AgentArgumentError("当前账号已不再具备管理员权限")
            result = await tool.handler(runtime, user_id, arguments, str(pending["session_key"]))
        except _SERVICE_ERRORS as exc:
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
            raw = await runtime.llm.chat(messages, temperature=0.0, max_tokens=400)
        except Exception as exc:  # noqa: BLE001 —— 工具解析失败应回退为普通聊天
            logger.warning("agent plan failed error=%s", type(exc).__name__)
            return None
        return _parse_plan(raw, self.tool_names)

    def _build_tools(self) -> dict[str, AgentTool]:
        return {
            "github_add_repository": AgentTool(
                name="github_add_repository",
                description="将一个 GitHub 仓库加入当前用户的监控列表。",
                parameters=_repo_parameters(),
                read_only=False,
                handler=_github_add,
            ),
            "github_remove_repository": AgentTool(
                name="github_remove_repository",
                description="从当前用户的 GitHub 监控列表移除一个仓库。",
                parameters=_repo_parameters(),
                read_only=False,
                handler=_github_remove,
            ),
            "github_list_repositories": AgentTool(
                name="github_list_repositories",
                description="查看当前用户已经加入的 GitHub 监控仓库。",
                parameters=_empty_parameters(),
                read_only=True,
                handler=_github_list,
            ),
            "github_check_repository": AgentTool(
                name="github_check_repository",
                description="检查当前用户已加入的 GitHub 仓库是否有新变化。",
                parameters=_repo_parameters(),
                read_only=True,
                handler=_github_check,
            ),
            "github_info_repository": AgentTool(
                name="github_info_repository",
                description="查询一个 GitHub 仓库的公开信息。",
                parameters=_repo_parameters(),
                read_only=True,
                handler=_github_info,
            ),
            "github_watch_repository": AgentTool(
                name="github_watch_repository",
                description="给当前用户已加入的 GitHub 仓库添加 user:QQ号 或 group:群号 通知目标。",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "GitHub 仓库 URL 或 owner/repo"},
                        "target": {"type": "string", "description": "user:QQ号 或 group:群号"},
                    },
                    "required": ["url", "target"],
                    "additionalProperties": False,
                },
                read_only=False,
                handler=_github_watch,
            ),
            "github_digest_set": AgentTool(
                name="github_digest_set",
                description="设置 GitHub 定时汇总的 user:QQ号 或 group:群号 收件人。",
                parameters=_targets_parameters(),
                read_only=False,
                handler=_github_digest_set,
                requires_admin=True,
            ),
            "github_digest_list": AgentTool(
                name="github_digest_list",
                description="查看 GitHub 定时汇总收件人。",
                parameters=_empty_parameters(),
                read_only=True,
                handler=_github_digest_list,
                requires_admin=True,
            ),
            "github_digest_clear": AgentTool(
                name="github_digest_clear",
                description="清空 GitHub 定时汇总收件人并关闭定时汇总。",
                parameters=_empty_parameters(),
                read_only=False,
                handler=_github_digest_clear,
                requires_admin=True,
            ),
            "memory_save": AgentTool(
                name="memory_save",
                description="保存一条当前用户主动要求记住的长期记忆。",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_type": {"type": "string", "enum": sorted(VALID_MEMORY_TYPES)},
                        "content": {"type": "string"},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                read_only=False,
                handler=_memory_save,
            ),
            "memory_list": AgentTool(
                name="memory_list",
                description="查看当前用户的长期记忆。",
                parameters=_empty_parameters(),
                read_only=True,
                handler=_memory_list,
            ),
            "notification_set": AgentTool(
                name="notification_set",
                description="开启或关闭当前用户的 reminder、GitHub 或日报通知。",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": ["reminder", "github", "report"]},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name", "enabled"],
                    "additionalProperties": False,
                },
                read_only=False,
                handler=_notification_set,
            ),
            "reminder_create": AgentTool(
                name="reminder_create",
                description="为当前用户创建一次性或 cron 提醒；timing 必须是 YYYY-MM-DD HH:MM 或 cron:五段表达式。",
                parameters={
                    "type": "object",
                    "properties": {
                        "timing": {"type": "string", "description": "YYYY-MM-DD HH:MM 或 cron:0 8 * * *"},
                        "message": {"type": "string"},
                    },
                    "required": ["timing", "message"],
                    "additionalProperties": False,
                },
                read_only=False,
                handler=_reminder_create,
            ),
            "schedule_group_message": AgentTool(
                name="schedule_group_message",
                description="管理员创建一次性或 cron 定时群发固定消息。",
                parameters=_schedule_parameters("message"),
                read_only=False,
                handler=_schedule_group_message,
                requires_admin=True,
            ),
            "schedule_topic_joke": AgentTool(
                name="schedule_topic_joke",
                description="管理员创建按主题定时生成不同段子的群任务。",
                parameters=_schedule_parameters("topic"),
                read_only=False,
                handler=_schedule_topic_joke,
                requires_admin=True,
            ),
            "broadcast_message": AgentTool(
                name="broadcast_message",
                description="管理员向一个或多个 user:QQ号、group:群号发送消息。",
                parameters={
                    "type": "object",
                    "properties": {
                        "targets": {"type": "string", "description": "逗号分隔的 user:QQ号 或 group:群号"},
                        "message": {"type": "string"},
                    },
                    "required": ["targets", "message"],
                    "additionalProperties": False,
                },
                read_only=False,
                handler=_broadcast_message,
                requires_admin=True,
            ),
            "clear_session": AgentTool(
                name="clear_session",
                description="清空当前用户在当前私聊或群聊中的 AI 会话。",
                parameters=_empty_parameters(),
                read_only=False,
                handler=_clear_session,
            ),
            "status": AgentTool(
                name="status",
                description="查看 Bot 运行状态，不包含密钥。",
                parameters=_empty_parameters(),
                read_only=True,
                handler=_status,
            ),
            "daily_report": AgentTool(
                name="daily_report",
                description="查看当前用户的今日日报。",
                parameters=_empty_parameters(),
                read_only=True,
                handler=_daily_report,
            ),
        }

    def _normalize_arguments(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        runtime: Any,
        *,
        history: list[dict] | None = None,
        request_text: str = "",
    ) -> dict[str, Any] | None:
        if not isinstance(arguments, Mapping):
            return None
        if tool_name in _EMPTY_TOOLS:
            return {} if not arguments else None
        if tool_name in {"github_add_repository", "github_remove_repository", "github_check_repository", "github_info_repository"}:
            return {"url": self._normalize_repo(arguments, history=history, request_text=request_text)}
        if tool_name == "github_watch_repository":
            if set(arguments) != {"url", "target"}:
                return None
            url = self._normalize_repo({"url": arguments.get("url")}, history=history, request_text=request_text)
            target = self._normalize_target(arguments.get("target"))
            return {"url": url, "target": target}
        if tool_name == "github_digest_set":
            if set(arguments) != {"targets"}:
                return None
            return {"targets": self._normalize_targets(arguments, runtime)}
        if tool_name == "memory_save":
            if set(arguments) - {"memory_type", "content"} or "content" not in arguments:
                return None
            content = str(arguments["content"]).strip()
            memory_type = str(arguments.get("memory_type", "fact") or "fact").strip().lower()
            if not content or memory_type not in VALID_MEMORY_TYPES:
                raise AgentArgumentError("记忆类型或内容无效")
            if len(content) > _MAX_AGENT_TEXT_CHARS:
                raise AgentArgumentError(f"记忆内容不能超过 {_MAX_AGENT_TEXT_CHARS} 个字符")
            return {"memory_type": memory_type, "content": content}
        if tool_name == "notification_set":
            if set(arguments) != {"name", "enabled"}:
                return None
            names = {"reminder": "reminder_notify", "github": "github_notify", "report": "daily_report"}
            name = str(arguments["name"]).strip().lower()
            enabled = arguments["enabled"]
            if name not in names or not isinstance(enabled, bool):
                raise AgentArgumentError("通知项应为 reminder、github 或 report，enabled 必须是布尔值")
            return {"field": names[name], "enabled": enabled}
        if tool_name == "reminder_create":
            return self._normalize_timing_and_text(arguments, runtime, "message")
        if tool_name in {"schedule_group_message", "schedule_topic_joke"}:
            text_key = "message" if tool_name == "schedule_group_message" else "topic"
            if set(arguments) != {"group_id", "timing", text_key}:
                return None
            group_id = str(arguments["group_id"]).strip()
            content = str(arguments[text_key]).strip()
            timing = str(arguments["timing"]).strip()
            if not group_id.isdigit() or not content:
                raise AgentArgumentError("群号必须是数字，内容不能为空")
            self._validate_timing(timing, content, runtime)
            return {"group_id": group_id, "timing": timing, text_key: content}
        if tool_name == "broadcast_message":
            if set(arguments) != {"targets", "message"}:
                return None
            message = str(arguments["message"]).strip()
            if not message:
                raise AgentArgumentError("消息内容不能为空")
            targets = self._normalize_targets(arguments, runtime)
            return {"targets": targets, "message": message}
        return None

    @staticmethod
    def _normalize_repo(
        arguments: Mapping[str, Any], *, history: list[dict] | None = None, request_text: str = ""
    ) -> str:
        if set(arguments) - {"url"}:
            raise AgentArgumentError("需要一个 GitHub 仓库 URL 或 owner/repo")
        value = str(arguments.get("url", "")).strip()
        if not value or _is_repo_referent(value):
            value = _latest_github_url([*(history or []), {"role": "user", "content": request_text}]) or ""
        else:
            urls = extract_urls(value)
            if urls:
                value = urls[0]
        if _GITHUB_REPO_SHORTHAND.fullmatch(value):
            value = f"https://github.com/{value}"
        try:
            return parse_repo_url(value).url
        except GitHubTrackerError as exc:
            raise AgentArgumentError(str(exc)) from exc

    @staticmethod
    def _normalize_target(value: Any) -> str:
        target = str(value or "").strip()
        target_type, separator, target_id = target.partition(":")
        if not separator or target_type.lower() not in {"user", "group"} or not target_id.isdigit():
            raise AgentArgumentError("通知目标应为 user:QQ号 或 group:群号")
        return f"{target_type.lower()}:{target_id}"

    @staticmethod
    def _normalize_targets(arguments: Mapping[str, Any], runtime: Any) -> str:
        raw = str(arguments.get("targets", "")).strip()
        try:
            targets = parse_targets(raw)
        except BroadcastFormatError as exc:
            raise AgentArgumentError(str(exc)) from exc
        if len(targets) > runtime.settings.max_broadcast_recipients:
            raise AgentArgumentError(
                f"目标数量 {len(targets)} 超过上限 {runtime.settings.max_broadcast_recipients}"
            )
        return ",".join(target.display() for target in targets)

    @staticmethod
    def _normalize_timing_and_text(arguments: Mapping[str, Any], runtime: Any, text_key: str) -> dict[str, str] | None:
        if set(arguments) != {"timing", text_key}:
            return None
        timing = str(arguments["timing"]).strip()
        content = str(arguments[text_key]).strip()
        if not timing or not content:
            raise AgentArgumentError("时间和内容不能为空")
        AgentHarness._validate_timing(timing, content, runtime)
        return {"timing": timing, text_key: content}

    @staticmethod
    def _validate_timing(timing: str, content: str, runtime: Any) -> None:
        try:
            timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
            parse_reminder_command(f"/remind {timing} -- {content}", timezone_info)
        except (SchedulerValidationError, ValueError) as exc:
            raise AgentArgumentError(str(exc)) from exc

    @staticmethod
    def _needs_admin(tool: AgentTool, arguments: Mapping[str, Any]) -> bool:
        if tool.requires_admin:
            return True
        return tool.name == "github_watch_repository" and str(arguments.get("target", "")).startswith("group:")

    @staticmethod
    def _missing_arguments_message(tool_name: str) -> str:
        messages = {
            "github": "请提供完整的 GitHub 仓库链接或 owner/repo。",
            "memory_save": "请告诉我要记住的具体内容。",
            "notification_set": "请说明要开启或关闭哪类通知。",
            "reminder_create": "请提供提醒时间和内容，例如“明天 8 点提醒我提交周报”。",
            "schedule_group_message": "请提供群号、时间和要发送的内容。",
            "schedule_topic_joke": "请提供群号、时间和段子主题。",
            "broadcast_message": "请提供发送目标和消息内容。",
        }
        if tool_name.startswith("github_"):
            return messages["github"]
        return messages.get(tool_name, "我需要更明确的信息才能执行这个操作。")

    @staticmethod
    def _summary(tool_name: str, arguments: dict[str, Any]) -> str:
        summaries = {
            "github_add_repository": "把 {url} 加入你的 GitHub 监控列表",
            "github_remove_repository": "从你的 GitHub 监控列表移除 {url}",
            "github_watch_repository": "为 {url} 添加通知目标 {target}",
            "github_digest_set": "把 GitHub 定时汇总发送给 {targets}",
            "github_digest_clear": "清空 GitHub 定时汇总收件人并关闭定时汇总",
            "memory_save": "保存一条长期记忆：{content}",
            "notification_set": "{field}通知{enabled_label}",
            "reminder_create": "创建提醒：{timing} —— {message}",
            "schedule_group_message": "向群 {group_id} 按 {timing} 发送：{message}",
            "schedule_topic_joke": "在群 {group_id} 按 {timing} 讲 {topic} 主题段子",
            "broadcast_message": "向 {targets} 发送消息：{message}",
            "clear_session": "清空当前 AI 会话",
        }
        value = dict(arguments)
        if tool_name == "notification_set":
            value["enabled_label"] = "开启" if value["enabled"] else "关闭"
            value["field"] = {"reminder_notify": "提醒", "github_notify": "GitHub", "daily_report": "日报"}[value["field"]]
        template = summaries.get(tool_name, f"执行 {tool_name}")
        return truncate_for_qq(template.format(**value), 800)


_EMPTY_TOOLS = frozenset(
    {
        "github_list_repositories",
        "github_digest_list",
        "github_digest_clear",
        "memory_list",
        "clear_session",
        "status",
        "daily_report",
    }
)
_REPO_REFERENTS = frozenset(
    {"这个仓库", "该仓库", "上面那个仓库", "刚才那个仓库", "这个repo", "该repo", "the repo", "it"}
)
_NORMALIZED_REPO_REFERENTS = frozenset("".join(item.lower().split()) for item in _REPO_REFERENTS)
_SERVICE_ERRORS = (
    GitHubAPIError,
    GitHubTrackerError,
    MemoryValidationError,
    NotificationSettingsError,
    BroadcastFormatError,
    ReportError,
    SchedulerValidationError,
    AgentArgumentError,
    TypeError,
    ValueError,
    json.JSONDecodeError,
)


def _empty_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _repo_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "完整的 GitHub 仓库 URL 或 owner/repo"}},
        "required": ["url"],
        "additionalProperties": False,
    }


def _targets_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"targets": {"type": "string", "description": "逗号分隔的 user:QQ号 或 group:群号"}},
        "required": ["targets"],
        "additionalProperties": False,
    }


def _schedule_parameters(content_name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "数字群号"},
            "timing": {"type": "string", "description": "YYYY-MM-DD HH:MM 或 cron:0 8 * * *"},
            content_name: {"type": "string"},
        },
        "required": ["group_id", "timing", content_name],
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


def _is_repo_referent(value: str) -> bool:
    normalized = "".join(value.lower().split())
    return normalized in _NORMALIZED_REPO_REFERENTS


def _latest_github_url(history: list[dict]) -> str | None:
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        for url in reversed(extract_urls(str(item.get("content", "")))):
            try:
                return parse_repo_url(url).url
            except GitHubTrackerError:
                continue
    return None


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


async def _github_add(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    repo = await runtime.github.add_repository(user_id, arguments["url"])
    return f'已添加仓库 {repo["repo_owner"]}/{repo["repo_name"]}。'


async def _github_remove(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    if not await runtime.github.remove_repository(user_id, arguments["url"]):
        raise GitHubTrackerError("该仓库尚未添加")
    return "已移除仓库。"


async def _github_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    repos = await runtime.github.list_repositories(user_id)
    if not repos:
        return "你还没有添加 GitHub 仓库。"
    return "你的 GitHub 仓库：\n" + "\n".join(
        f'{repo["repo_owner"]}/{repo["repo_name"]}' for repo in repos
    )


async def _github_check(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    return format_check_result(await runtime.github.check(user_id, arguments["url"]))


async def _github_info(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    ref = parse_repo_url(arguments["url"])
    info = await runtime.github_client.get_repository(ref.owner, ref.name)
    return (
        f"{ref.owner}/{ref.name}\n"
        f"Star：{info.get('stargazers_count', 0)}  Fork：{info.get('forks_count', 0)}\n"
        f"开放 Issue：{info.get('open_issues_count', 0)}\n{info.get('html_url', ref.url)}"
    )


async def _github_watch(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    await runtime.github.watch(user_id, arguments["url"], arguments["target"])
    return "GitHub 通知目标已添加。"


async def _github_digest_set(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    targets = parse_targets(arguments["targets"])
    await runtime.db.replace_github_digest_targets([target.to_dict() for target in targets])
    await runtime.scheduler.sync_system_task("github_digest", runtime.settings.github_digest_cron)
    return f"GitHub 定时汇总目标已更新：{arguments['targets']}。"


async def _github_digest_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments, session_key
    targets = await runtime.db.fetch_github_digest_targets()
    if not targets:
        return "尚未设置 GitHub 定时汇总目标。"
    return "GitHub 定时汇总目标：\n" + "\n".join(
        f'{target["target_type"]}:{target["target_id"]}' for target in targets
    )


async def _github_digest_clear(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments, session_key
    await runtime.db.replace_github_digest_targets([])
    await runtime.scheduler.sync_system_task("github_digest", "")
    return "GitHub 定时汇总目标已清空，定时发送已关闭。"


async def _memory_save(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    memory_id = await runtime.memory.save(user_id, arguments["memory_type"], arguments["content"])
    if memory_id is None:
        return "这条记忆的重要度太低，未保存。"
    return f"已保存长期记忆（{arguments['memory_type']}，编号 {memory_id}）。"


async def _memory_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    memories = await runtime.memory.list_for_context(user_id, limit=20)
    if not memories:
        return "暂时没有长期记忆。"
    return "你的长期记忆：\n" + "\n".join(
        f"{memory['id']}. [{memory['type']}] {memory['content']}" for memory in memories
    )


async def _notification_set(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    await runtime.notifications.set(user_id, **{arguments["field"]: arguments["enabled"]})
    label = {"reminder_notify": "提醒", "github_notify": "GitHub", "daily_report": "日报"}[arguments["field"]]
    return f"{label}通知已{'开启' if arguments['enabled'] else '关闭'}。"


async def _reminder_create(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    if not (await runtime.notifications.get(user_id))["reminder_notify"]:
        raise SchedulerValidationError("提醒通知默认关闭，请先开启提醒通知")
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['message']}", timezone_info
    )
    task_id = await runtime.scheduler.create_reminder(
        user_id, arguments["message"], run_at=run_at, cron_expression=cron_expression
    )
    return f"提醒已创建（编号 {task_id}）。"


async def _schedule_group_message(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['message']}", timezone_info
    )
    task_id = await runtime.scheduler.create_group_message(
        user_id, arguments["group_id"], arguments["message"], run_at=run_at, cron_expression=cron_expression
    )
    return f"定时群发已创建（编号 {task_id}），目标群：{arguments['group_id']}。"


async def _schedule_topic_joke(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['topic']}", timezone_info
    )
    task_id = await runtime.scheduler.create_topic_joke(
        user_id, arguments["group_id"], arguments["topic"], run_at=run_at, cron_expression=cron_expression
    )
    return f"主题段子任务已创建（编号 {task_id}），目标群：{arguments['group_id']}。"


async def _broadcast_message(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    targets = parse_targets(arguments["targets"])
    report = await runtime.dispatcher.broadcast(targets, arguments["message"])
    return report.summary_text()


async def _clear_session(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments
    await runtime.sessions.clear(session_key)
    return "当前会话已清空。"


async def _status(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments, session_key
    status = await runtime.health.check()
    counts = status["counts"]
    errors = status["recent_errors"]
    return (
        "qq-llm-bot 运行状态：\n"
        f"运行时间：{status['uptime_seconds']} 秒\n"
        f"LLM：{'正常' if status['llm']['ok'] else '异常'}\n"
        f"数据库：{'正常' if status['database']['ok'] else '异常'}\n"
        f"Scheduler：{'运行中' if status['scheduler']['running'] else '未运行'}（{status['scheduler']['jobs']} 个任务）\n"
        f"GitHub 监控：{counts.get('github_repositories', 0)}\n"
        f"Memory：{counts.get('memories', 0)}\n"
        f"最近错误：{errors[0]['error'] if errors else '无'}"
    )


async def _daily_report(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    return truncate_for_qq(await runtime.report.build_daily_report(user_id))
