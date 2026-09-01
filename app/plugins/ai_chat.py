"""AI 聊天插件：私聊直接进入 LLM；群聊仅 @机器人 或 /ai 触发；/clear 清空会话。"""

from collections import OrderedDict

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from app.promptlib import load_prompt
from app.services.llm.base import LLMError
from app.services.runtime import get_runtime
from app.services.session.manager import SessionManager
from app.utils import truncate_for_qq

CHAT_SYSTEM_PROMPT = load_prompt("chat.txt")

# 已知命令白名单：私聊中其他 "/xxx" 一律视为未知命令，而不是丢给 LLM
KNOWN_COMMANDS = {
    "/ai", "/clear", "/总结", "/summary", "/broadcast",
    "/confirm", "/cancel", "/status", "/状态", "/help", "/帮助",
}

HELP_TEXT = (
    "可用命令：\n"
    "/ai <问题> —— 群内向 AI 提问\n"
    "/clear —— 清空当前会话上下文\n"
    "/总结 <URL> —— 总结网页（/summary 同义）\n"
    "/help —— 显示本帮助\n"
    "私聊直接发消息即可对话；群里 @我 或回复我也可以。\n"
    "管理员额外命令：/broadcast 目标列表 -- 消息、/confirm、/cancel、/status"
)

# message_id 去重（LRU），防止 OneBot 重复投递导致重复处理
_DEDUP: OrderedDict[str, None] = OrderedDict()
_DEDUP_MAX = 4096


def _claim(message_id: str) -> bool:
    if message_id in _DEDUP:
        return False
    _DEDUP[message_id] = None
    if len(_DEDUP) > _DEDUP_MAX:
        _DEDUP.popitem(last=False)
    return True


def _is_self(event: MessageEvent) -> bool:
    return str(event.user_id) == str(event.self_id)


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
        await matcher.send(HELP_TEXT)
        return
    if text.startswith("/") and first_token not in KNOWN_COMMANDS:
        await matcher.send(f"未知命令 {first_token}。\n\n{HELP_TEXT}")
        return
    if text == "/clear":
        await runtime.sessions.clear(session_key)
        await matcher.send("当前会话已清空。")
        return
    if not text:
        await matcher.send("有什么想问的？直接发消息，或用 /ai <问题>。")
        return

    history = await runtime.sessions.get_context(session_key)
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + history + [{"role": "user", "content": text}]

    logger.info("LLM chat session=%s user=%s group=%s msg=%s",
                session_key, event.user_id, getattr(event, "group_id", "-"), event.message_id)
    try:
        reply = await runtime.llm.chat(messages)
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
        await meta_matcher.send("当前会话已清空。")
    else:
        await meta_matcher.send(HELP_TEXT)


# 供其他插件复用的去重声明
claim_message_id = _claim
