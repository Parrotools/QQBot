"""GitHub 仓库登记、快照和变化检测。"""

import logging
import re
from urllib.parse import urlparse

from app.database.db import Database
from app.services.github.models import GitHubRepoRef, GitHubSnapshot
from app.services.notifications import NotificationSettingsService

logger = logging.getLogger(__name__)

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class GitHubTrackerError(ValueError):
    """GitHub 仓库或监控操作不合法。"""


def parse_repo_url(url: str) -> GitHubRepoRef:
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_HOSTS:
        raise GitHubTrackerError("只支持 https://github.com/owner/repo 格式")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubTrackerError("GitHub 仓库 URL 必须包含 owner 和 repo")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not _NAME_PATTERN.fullmatch(owner) or not _NAME_PATTERN.fullmatch(name):
        raise GitHubTrackerError("GitHub 仓库名称无效")
    return GitHubRepoRef(owner, name, f"https://github.com/{owner}/{name}")


def compare_snapshots(previous: dict | None, current: dict | GitHubSnapshot) -> list[dict]:
    if previous is None:
        return []
    current_data = current.to_dict() if isinstance(current, GitHubSnapshot) else current
    changes: list[dict] = []
    if previous["latest_commit_sha"] != current_data["latest_commit_sha"]:
        changes.append(
            {
                "type": "commit",
                "sha": current_data["latest_commit_sha"],
                "message": current_data["latest_commit_message"],
                "author": current_data["latest_commit_author"],
                "time": current_data["latest_commit_time"],
            }
        )
    for field, change_type in (("stars", "stars"), ("forks", "forks"), ("open_issues_count", "issues")):
        changed = (
            current_data[field] != previous[field]
            if field == "open_issues_count"
            else current_data[field] > previous[field]
        )
        if changed:
            changes.append(
                {"type": change_type, "from": previous[field], "to": current_data[field], "delta": current_data[field] - previous[field]}
            )
    if current_data["latest_release"] != previous["latest_release"] and current_data["latest_release"]:
        changes.append({"type": "release", "from": previous["latest_release"], "to": current_data["latest_release"]})
    return changes


class GitHubTracker:
    def __init__(self, db: Database, client, dispatcher=None, notifications=None):
        self._db = db
        self._client = client
        self._dispatcher = dispatcher
        self._notifications = notifications or NotificationSettingsService(db)

    async def add_repository(self, owner_id: str, url: str) -> dict:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise GitHubTrackerError("owner_id 不能为空")
        ref = parse_repo_url(url)
        repo_id = await self._db.insert_github_repository(owner_id, ref.owner, ref.name, ref.url)
        row = await self._db.fetch_github_repository(str(owner_id), ref.owner, ref.name)
        return {**row, "id": repo_id}

    async def remove_repository(self, owner_id: str, url: str) -> bool:
        ref = parse_repo_url(url)
        return await self._db.delete_github_repository(str(owner_id), ref.owner, ref.name)

    async def list_repositories(self, owner_id: str) -> list[dict]:
        return await self._db.fetch_github_repositories(str(owner_id))

    async def check(self, owner_id: str, url: str) -> dict:
        repo = await self._get_owned_repository(owner_id, url)
        return await self._check_row(repo)

    async def run_scheduled_check(self, task: dict) -> None:
        if self._dispatcher is None:
            raise GitHubTrackerError("GitHub 通知未配置消息发送器")
        for repo in await self._db.fetch_github_repositories():
            try:
                result = await self._check_row(repo)
            except Exception:
                logger.exception("GitHub 检查失败 repo_id=%s", repo["id"])
                continue
            if not result["changes"]:
                continue
            settings = await self._notifications.get(repo["owner_id"])
            if not settings["github_notify"]:
                continue
            message = format_check_result(result)
            for target in await self._db.fetch_github_notifications(repo["id"]):
                sender = self._dispatcher.enqueue_user if target["target_type"] == "user" else self._dispatcher.enqueue_group
                try:
                    await sender(target["target_id"], message)
                except Exception:
                    logger.exception("GitHub 通知发送失败 repo_id=%s target=%s:%s", repo["id"], target["target_type"], target["target_id"])

    async def _check_row(self, repo: dict) -> dict:
        snapshot = await self._client.fetch_snapshot(
            GitHubRepoRef(repo["repo_owner"], repo["repo_name"], repo["repo_url"])
        )
        previous = await self._db.fetch_latest_github_snapshot(repo["id"])
        changes = compare_snapshots(previous, snapshot)
        await self._db.insert_github_snapshot(repo["id"], snapshot.to_dict())
        return {"repo": repo, "snapshot": snapshot, "changes": changes}

    async def watch(self, owner_id: str, url: str, target: str) -> None:
        repo = await self._get_owned_repository(owner_id, url)
        target_type, separator, target_id = target.partition(":")
        if not separator or target_type not in ("user", "group") or not target_id.isdigit():
            raise GitHubTrackerError("通知目标应为 user:QQ号 或 group:群号")
        await self._db.upsert_github_notification(repo["id"], target_type, target_id)

    async def _get_owned_repository(self, owner_id: str, url: str) -> dict:
        ref = parse_repo_url(url)
        repo = await self._db.fetch_github_repository(str(owner_id), ref.owner, ref.name)
        if repo is None:
            raise GitHubTrackerError("该仓库尚未添加")
        return repo


def format_check_result(result: dict) -> str:
    repo = result["repo"]
    name = f'{repo["repo_owner"]}/{repo["repo_name"]}'
    changes = result["changes"]
    if not changes:
        return f"已检查 {name}：暂无新变化。"
    lines = [f"{name} 有新变化："]
    for change in changes:
        if change["type"] == "commit":
            lines.append(f'提交：{change["message"]}（{change["author"]}）')
        elif change["type"] == "release":
            lines.append(f'发布版本：{change["to"]}')
        else:
            labels = {"stars": "Star", "forks": "Fork", "issues": "开放 Issue"}
            lines.append(f'{labels[change["type"]]}：{change["from"]} → {change["to"]}')
    return "\n".join(lines)
