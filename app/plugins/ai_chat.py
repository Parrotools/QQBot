"""AI 聊天插件：私聊直接进入 LLM；群聊仅 @机器人 或 /ai 触发；/clear 清空会话。"""

from collections import OrderedDict

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from app.personality.manager import PersonaContext
from app.promptlib import load_prompt
from app.services.llm.base import LLMError
from app.services.runtime import get_runtime
from app.services.session.manager import SessionManager
from app.utils import send_local_reply, truncate_for_qq

CHAT_SYSTEM_PROMPT = load_prompt("chat.txt")

# 已知命令白名单：私聊中其他 "/xxx" 一律视为未知命令，而不是丢给 LLM
KNOWN_COMMANDS = {
    "/ai", "/clear", "/总结", "/summary", "/broadcast",
    "/confirm", "/cancel", "/status", "/状态", "/help", "/帮助", "/remember", "/memories", "/memory",
    "/remind", "/notify", "/report", "/日报",
}
_DELEGATED_COMMANDS = {
    "/总结", "/summary", "/broadcast", "/confirm", "/cancel", "/status", "/状态",
    "/remember", "/memories", "/memory", "/remind", "/notify", "/github", "/report", "/日报",
}

HELP_TEXT = (
    "可用命令：\n"
    "/ai <问题> —— 群内向 AI 提问\n"
    "/clear —— 清空当前会话上下文\n"
    "/总结 <URL> —— 总结网页（/summary 同义）\n"
    "/help —— 显示本帮助\n"
    "/remember [类型] -- 内容 —— 保存长期记忆\n"
    "/memories —— 查看我的长期记忆\n"
    "/remind 时间或 cron -- 内容 —— 创建提醒\n"
    "/notify reminder|github on|off —— 开关提醒或 GitHub 通知\n"
    "/github add|remove|list|check|info|watch —— GitHub 仓库监控\n"
    "/report —— 查看今日汇总\n"
    "私聊直接发消息即可对话；群里 @我 或回复我也可以。\n"
    "管理员额外命令：/broadcast 目标列表 -- 消息、/confirm、/cancel、/status"
)

# message_id 去重（LRU），防止 OneBot 重复投递导致重复处理
_DEDUP: OrderedDict[str, None] = OrderedDict()
_DEDUP_MAX = 4096

_TECHNICAL_HINTS = (
    "代码", "报错", "bug", "debug", "编译", "运行", "算法", "复杂度", "排序", "函数", "类", "接口",
    "python", "java", "javascript", "typescript", "c++", "rust", "go语言", "linux", "git", "github",
    "sql", "数据库", "网络", "协议", "并发", "线程", "进程", "性能", "优化", "部署", "命令行",
    "traceback", "exception", "error", "benchmark", "cuda", "mpi", "openmp", "simd",
    "```",
)

_INTIMATE_HINTS = (
    "喜欢我", "喜欢你", "爱我", "爱你", "想我", "想你", "交往", "在一起", "约会", "表白", "告白",
    "男朋友", "女朋友", "对象", "抱抱", "亲亲", "亲一下", "陪我睡", "想和你",
)

_OWNER_IDENTITY_QUESTIONS = (
    "你认识我吗",
    "你还认识我吗",
    "你记得我吗",
    "你还记得我吗",
    "你知道我是谁吗",
    "我是谁",
    "我是你的主人吗",
    "你的主人是谁",
    "谁是你的主人",
)
_OWNER_MANAGER_HINTS = (
    "谁管理你",
    "谁在管理你",
    "谁管理着你",
    "谁管你",
    "谁在管你",
    "由谁管理",
    "你由谁管理",
    "你是由谁管理的",
    "你是谁管理的",
    "管理员是谁",
    "管理者是谁",
    "谁是管理员",
    "管理你的人是谁",
    "你的管理员",
    "谁管理这个机器人",
    "谁管理这个bot",
    "谁管理rumi",
    "谁在负责管理你",
)


def _claim(message_id: str) -> bool:
    if message_id in _DEDUP:
        return False
    _DEDUP[message_id] = None
    if len(_DEDUP) > _DEDUP_MAX:
        _DEDUP.popitem(last=False)
    return True


def _is_self(event: MessageEvent) -> bool:
    return str(event.user_id) == str(event.self_id)


def _is_owner(event: MessageEvent, runtime) -> bool:
    """只用配置中的 QQ ID 识别主人，昵称、群名片和自述都不参与判断。"""
    configured_owner = getattr(runtime.settings, "owner_id", "")
    if not configured_owner:
        configured_owner = getattr(runtime.settings, "owner_qq_id", "")
    return bool(str(configured_owner).strip()) and str(event.user_id) == str(configured_owner).strip()


def _conversation_mode(text: str) -> str:
    lowered = text.lower()
    if any(hint in lowered for hint in _INTIMATE_HINTS):
        return "intimate"
    return "technical" if any(hint in lowered for hint in _TECHNICAL_HINTS) else "casual"


def _persona_context(event: MessageEvent, text: str, runtime) -> PersonaContext:
    sender = getattr(event, "sender", None)
    sender_name = getattr(sender, "card", None) or getattr(sender, "nickname", "") or ""
    settings = runtime.settings
    return PersonaContext(
        relationship="owner" if _is_owner(event, runtime) else "normal",
        mode=_conversation_mode(text),
        conversation_mood="normal",
        sender_name=str(sender_name),
        owner_name=str(getattr(settings, "owner_name", "Parrotools") or "Parrotools"),
    )


def _owner_identity_reply(event: MessageEvent, text: str, runtime) -> str | None:
    """身份问题使用已核验的事件身份回答，不让 LLM 否认确定事实。"""
    if not _is_owner(event, runtime):
        return None
    normalized = "".join(text.lower().split()).rstrip("?？！!。~～")
    is_identity_question = normalized in _OWNER_IDENTITY_QUESTIONS
    is_manager_question = any(hint in normalized for hint in _OWNER_MANAGER_HINTS)
    if not is_identity_question and not is_manager_question:
        return None
    owner_name = " ".join(str(getattr(runtime.settings, "owner_name", "") or "Parrotools").split())
    owner_name = owner_name[:64] or "Parrotools"
    if is_manager_question:
        return f"当然知道呀，是 {owner_name} 在管理 Rumi。你就是我的主人，这件事我记得很清楚。"
    return f"当然认识呀，你是 {owner_name}，我的主人。刚才我把“不知道私人细节”和“不认识你”混在一起了，是我说错啦。"


def _temperature(runtime, mode: str) -> float:
    """给闲聊一点表达空间，技术问答收窄随机性；不改变 provider 的接口契约。"""
    base = float(getattr(runtime.settings, "llm_temperature", 0.9))
    base = max(0.0, min(2.0, base))
    delta = 0.1 if mode in {"casual", "intimate"} else -0.1
    return max(0.0, min(2.0, base + delta))


def _build_system_prompt(runtime, context: PersonaContext, memory_context: str) -> str:
    """兼容旧的轻量测试替身，同时让正式 PersonalityManager 收到完整情境。"""
    builder = runtime.personality.build_system_prompt
    try:
        return builder(CHAT_SYSTEM_PROMPT, context=context, memory_context=memory_context)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        prompt = builder(CHAT_SYSTEM_PROMPT)
        return f"{prompt}\n\n{memory_context.strip()}" if memory_context.strip() else prompt


def _build_chat_messages(
    runtime, event: MessageEvent, text: str, history: list[dict], memory_context: str
) -> tuple[list[dict], PersonaContext]:
    context = _persona_context(event, text, runtime)
    messages = [{
        "role": "system",
        "content": _build_system_prompt(runtime, context, memory_context),
    }]
    few_shot_builder = getattr(runtime.personality, "build_few_shot_messages", None)
    if few_shot_builder is not None:
        messages.extend(few_shot_builder(context))
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    return messages, context


def _session_key(event: MessageEvent) -> str:
    runtime = get_runtime()
    if isinstance(event, GroupMessageEvent):
        return SessionManager.group_key(
            str(event.group_id), str(event.user_id), runtime.settings.group_shared_context
        )
    return SessionManager.private_key(str(event.user_id))


async def _trigger(event: MessageEvent) -> bool:
    if _is_self(event):
        return False  # 过滤自己的消息，防循环
    text = event.message.extract_plain_text().strip()
    first_token = text.split(maxsplit=1)[0].lower() if text else ""
    if first_token in _DELEGATED_COMMANDS:
        return False
    if isinstance(event, GroupMessageEvent):
        # 群聊：仅 @机器人 / 回复机器人（to_me）或 "/ai ..." 触发
        matched = (event.to_me and bool(text)) or text == "/ai" or text.startswith("/ai ")
    else:
        matched = True  # 私聊默认直接进入 LLM
    if matched:
        return _claim(str(event.message_id))
    return False


matcher = on_message(rule=Rule(_trigger), priority=10, block=True)


@matcher.handle()
async def _handle(event: MessageEvent):
    runtime = get_runtime()
    session_key = _session_key(event)
    text = event.message.extract_plain_text().strip()

    # 私聊与群聊统一支持 /ai 前缀
    if text == "/ai" or text.startswith("/ai "):
        text = text[len("/ai"):].strip()

    first_token = text.split(maxsplit=1)[0].lower() if text else ""
    if first_token in ("/help", "/帮助"):
        await send_local_reply(matcher, runtime, HELP_TEXT)
        return
    if text.startswith("/") and first_token not in KNOWN_COMMANDS:
        await send_local_reply(matcher, runtime, f"未知命令 {first_token}。\n\n{HELP_TEXT}")
        return
    if text == "/clear":
        await runtime.sessions.clear(session_key)
        await send_local_reply(matcher, runtime, "当前会话已清空。")
        return
    if not text:
        await send_local_reply(matcher, runtime, "有什么想问的？直接发消息，或用 /ai <问题>。")
        return

    identity_reply = _owner_identity_reply(event, text, runtime)
    if identity_reply is not None:
        await runtime.sessions.append(session_key, "user", text)
        await runtime.sessions.append(session_key, "assistant", identity_reply)
        await send_local_reply(matcher, runtime, identity_reply)
        return

    history = await runtime.sessions.get_context(session_key)
    memory_context = await runtime.memory.context_prompt(str(event.user_id))
    messages, context = _build_chat_messages(runtime, event, text, history, memory_context)

    logger.info("LLM chat session=%s user=%s group=%s msg=%s",
                session_key, event.user_id, getattr(event, "group_id", "-"), event.message_id)
    try:
        reply = await runtime.llm.chat(messages, temperature=_temperature(runtime, context.mode))
    except LLMError:
        logger.exception("LLM 调用失败 session=%s", session_key)
        await matcher.send("AI 服务暂时不可用，请稍后再试。")
        return

    # 成功后才落库，失败轮次不污染上下文
    await runtime.sessions.append(session_key, "user", text)
    await runtime.sessions.append(session_key, "assistant", reply)
    await matcher.send(truncate_for_qq(reply))


# 群聊免 @ 的轻量命令：/clear、/help
async def _meta_trigger(event: GroupMessageEvent) -> bool:
    if _is_self(event):
        return False
    text = event.message.extract_plain_text().strip()
    if text in ("/clear", "/help", "/帮助"):
        return _claim(str(event.message_id))
    return False


meta_matcher = on_message(rule=Rule(_meta_trigger), priority=9, block=True)


@meta_matcher.handle()
async def _handle_meta(event: GroupMessageEvent):
    runtime = get_runtime()
    text = event.message.extract_plain_text().strip()
    if text == "/clear":
        await runtime.sessions.clear(_session_key(event))
        await send_local_reply(meta_matcher, runtime, "当前会话已清空。")
    else:
        await send_local_reply(meta_matcher, runtime, HELP_TEXT)


# 供其他插件复用的去重声明
claim_message_id = _claim
