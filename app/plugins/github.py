"""GitHub 仓库登记、手动检查和通知目标管理。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id, command_is_addressed, strip_bot_mention
from app.services.github.client import GitHubAPIError
from app.services.github.tracker import GitHubTrackerError, format_check_result, parse_repo_url
from app.services.runtime import get_runtime
from app.utils import send_local_reply

_GITHUB = "/github"
_HELP = (
    "用法：\n"
    "/github add <URL>\n"
    "/github remove <URL>\n"
    "/github list\n"
    "/github check <URL>\n"
    "/github info <URL>\n"
    "/github watch <URL> user:QQ号|group:群号"
)


def parse_github_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if len(parts) < 2 or parts[0].lower() != _GITHUB:
        raise GitHubTrackerError(_HELP)
    return parts[1].lower(), parts[2:]


async def _github_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text())
    if text.lower() == _GITHUB or text.lower().startswith(f"{_GITHUB} "):
        return claim_message_id(str(event.message_id))
    return False


github_matcher = on_message(rule=Rule(_github_rule), priority=6, block=True)


@github_matcher.handle()
async def _handle_github(event: MessageEvent):
    runtime = get_runtime()
    try:
        command, args = parse_github_command(strip_bot_mention(event.message.extract_plain_text()))
        if command == "add":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            repo = await runtime.github.add_repository(str(event.user_id), args[0])
            await send_local_reply(github_matcher, runtime, f'已添加仓库 {repo["repo_owner"]}/{repo["repo_name"]}。')
        elif command == "remove":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            if not await runtime.github.remove_repository(str(event.user_id), args[0]):
                raise GitHubTrackerError("该仓库尚未添加")
            await send_local_reply(github_matcher, runtime, "已移除仓库。")
        elif command == "list":
            if args:
                raise GitHubTrackerError(_HELP)
            repos = await runtime.github.list_repositories(str(event.user_id))
            if not repos:
                await send_local_reply(github_matcher, runtime, "你还没有添加 GitHub 仓库。")
                return
            await send_local_reply(github_matcher, runtime, "\n".join(f'{r["repo_owner"]}/{r["repo_name"]}' for r in repos))
        elif command == "check":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            await send_local_reply(
                github_matcher, runtime, format_check_result(await runtime.github.check(str(event.user_id), args[0]))
            )
        elif command == "info":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            ref = parse_repo_url(args[0])
            info = await runtime.github_client.get_repository(ref.owner, ref.name)
            await send_local_reply(
                github_matcher,
                runtime,
                f'{ref.owner}/{ref.name}\n'
                f'Star：{info.get("stargazers_count", 0)}  Fork：{info.get("forks_count", 0)}\n'
                f'开放 Issue：{info.get("open_issues_count", 0)}\n{info.get("html_url", ref.url)}'
            )
        elif command == "watch":
            if len(args) != 2:
                raise GitHubTrackerError(_HELP)
            target_type = args[1].partition(":")[0]
            if target_type == "group" and not runtime.permission.is_admin(str(event.user_id)):
                await send_local_reply(github_matcher, runtime, "群通知仅管理员可配置。")
                return
            await runtime.github.watch(str(event.user_id), args[0], args[1])
            await send_local_reply(github_matcher, runtime, "GitHub 通知目标已添加。")
        else:
            await send_local_reply(github_matcher, runtime, _HELP)
    except (GitHubTrackerError, GitHubAPIError) as e:
        await send_local_reply(github_matcher, runtime, f"GitHub 操作失败：{e}")
