"""aiosqlite 封装。

选型说明：当前只有少量表、单实例、低并发，SQLAlchemy async 属于过度设计；
这里用 aiosqlite + 本类提供的少量 repository 方法，业务层不直接写 SQL，
未来需要换 PostgreSQL/ORM 时只需替换本层。
"""

import json
from datetime import UTC
from pathlib import Path

import aiosqlite

from app.database.models import MIGRATIONS

_PENDING_ACTIVE = "active"
_PENDING_CONFIRMED = "confirmed"
_PENDING_CANCELLED = "cancelled"
_PENDING_EXPIRED = "expired"


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: str):
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._apply_migrations()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未连接，请先调用 connect()")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def _apply_migrations(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        rows = await self.fetchall("SELECT version FROM schema_migrations", ())
        applied = {int(row["version"]) for row in rows}
        for version, sql in MIGRATIONS:
            if version not in applied:
                await self.conn.executescript(sql)
                await self.conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                await self.conn.commit()

    async def migration_versions(self) -> list[int]:
        rows = await self.fetchall("SELECT version FROM schema_migrations ORDER BY version", ())
        return [int(row["version"]) for row in rows]

    # ---------- outbound_messages ----------

    async def enqueue_outbound_message(
        self, target_type: str, target_id: str, content: str, max_attempts: int
    ) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO outbound_messages "
            "(target_type, target_id, content, max_attempts, next_attempt_at) VALUES (?, ?, ?, ?, ?)",
            (target_type, target_id, content, max_attempts, _now()),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def claim_outbound_message(self, now: str, lease_before: str) -> dict | None:
        async with self.conn.execute(
            "SELECT id FROM outbound_messages WHERE "
            "((status IN ('pending', 'retry') AND next_attempt_at <= ?) "
            "OR (status = 'sending' AND locked_at <= ?)) ORDER BY id LIMIT 1",
            (now, lease_before),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        message_id = int(row["id"])
        await self.conn.execute(
            "UPDATE outbound_messages SET status = 'sending', attempts = attempts + 1, locked_at = ? "
            "WHERE id = ?",
            (now, message_id),
        )
        await self.conn.commit()
        return await self.fetchone("SELECT * FROM outbound_messages WHERE id = ?", (message_id,))

    async def mark_outbound_message_sent(self, message_id: int, sent_at: str, qq_message_id: str | None) -> None:
        await self.execute(
            "UPDATE outbound_messages SET status = 'sent', sent_at = ?, message_id = ?, locked_at = NULL, "
            "last_error = NULL WHERE id = ?",
            (sent_at, qq_message_id, message_id),
        )

    async def retry_outbound_message(
        self, message_id: int, attempts: int, max_attempts: int, next_attempt_at: str, error: str
    ) -> None:
        if attempts >= max_attempts:
            await self.execute(
                "UPDATE outbound_messages SET status = 'failed', locked_at = NULL, last_error = ? WHERE id = ?",
                (error[:500], message_id),
            )
            return
        await self.execute(
            "UPDATE outbound_messages SET status = 'retry', next_attempt_at = ?, locked_at = NULL, last_error = ? "
            "WHERE id = ?",
            (next_attempt_at, error[:500], message_id),
        )

    async def outbound_message_counts(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT status, COUNT(*) AS count FROM outbound_messages GROUP BY status", ())
        return {str(row["status"]): int(row["count"]) for row in rows}

    async def fetch_recent_errors(self, limit: int = 5) -> list[dict]:
        return await self.fetchall(
            "SELECT error, created_at FROM send_logs WHERE success = 0 ORDER BY id DESC LIMIT ?", (max(1, limit),)
        )

    # ---------- send_logs ----------

    async def insert_send_log(
        self,
        target_type: str,
        target_id: str,
        content: str,
        success: bool,
        message_id: str | None,
        error: str | None,
    ) -> None:
        await self.execute(
            "INSERT INTO send_logs (target_type, target_id, content, success, message_id, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target_type, target_id, content[:500], int(success), message_id, error[:500] if error else None),
        )

    # ---------- memories ----------

    async def insert_memory(self, user_id: str, memory_type: str, content: str, importance: float) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO memories (user_id, type, content, importance) VALUES (?, ?, ?, ?)",
            (user_id, memory_type, content, importance),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def fetch_memories(self, user_id: str, limit: int = 5) -> list[dict]:
        return await self.fetchall(
            "SELECT id, user_id, type, content, importance, created_at, updated_at "
            "FROM memories WHERE user_id = ? ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        )

    # ---------- scheduled_tasks ----------

    async def create_scheduled_task(
        self,
        owner_id: str,
        task_type: str,
        payload: dict,
        cron_expression: str | None,
        next_run: str,
    ) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO scheduled_tasks "
            "(owner_id, task_type, payload, cron_expression, next_run) VALUES (?, ?, ?, ?, ?)",
            (owner_id, task_type, json.dumps(payload, ensure_ascii=False), cron_expression, next_run),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def fetch_enabled_scheduled_tasks(self) -> list[dict]:
        return await self.fetchall("SELECT * FROM scheduled_tasks WHERE enabled = 1 ORDER BY id")

    async def fetch_scheduled_task(self, task_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))

    async def fetch_scheduled_task_by_owner_type(self, owner_id: str, task_type: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM scheduled_tasks WHERE owner_id = ? AND task_type = ? ORDER BY id DESC LIMIT 1",
            (owner_id, task_type),
        )

    async def mark_scheduled_task_run(
        self, task_id: int, last_run: str, next_run: str | None, enabled: bool
    ) -> None:
        await self.execute(
            "UPDATE scheduled_tasks SET last_run = ?, next_run = ?, enabled = ? WHERE id = ?",
            (last_run, next_run, int(enabled), task_id),
        )

    async def update_scheduled_task_schedule(
        self, task_id: int, cron_expression: str | None, next_run: str | None, enabled: bool
    ) -> None:
        await self.execute(
            "UPDATE scheduled_tasks SET cron_expression = ?, next_run = ?, enabled = ? WHERE id = ?",
            (cron_expression, next_run, int(enabled), task_id),
        )

    # ---------- notification_settings ----------

    async def fetch_notification_settings(self, user_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM notification_settings WHERE user_id = ?", (user_id,))

    async def save_notification_settings(
        self, user_id: str, github_notify: bool, daily_report: bool, reminder_notify: bool
    ) -> None:
        await self.execute(
            "INSERT INTO notification_settings "
            "(user_id, github_notify, daily_report, reminder_notify) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "github_notify = excluded.github_notify, "
            "daily_report = excluded.daily_report, "
            "reminder_notify = excluded.reminder_notify",
            (user_id, int(github_notify), int(daily_report), int(reminder_notify)),
        )

    # ---------- github ----------

    async def insert_github_repository(
        self, owner_id: str, repo_owner: str, repo_name: str, repo_url: str
    ) -> int:
        await self.conn.execute(
            "INSERT INTO github_repositories (owner_id, repo_owner, repo_name, repo_url) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(owner_id, repo_owner, repo_name) DO UPDATE SET repo_url = excluded.repo_url",
            (owner_id, repo_owner, repo_name, repo_url),
        )
        await self.conn.commit()
        row = await self.fetchone(
            "SELECT id FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
            (owner_id, repo_owner, repo_name),
        )
        return int(row["id"])

    async def fetch_github_repository(
        self, owner_id: str, repo_owner: str, repo_name: str
    ) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
            (owner_id, repo_owner, repo_name),
        )

    async def fetch_github_repositories(self, owner_id: str | None = None) -> list[dict]:
        if owner_id is None:
            return await self.fetchall("SELECT * FROM github_repositories ORDER BY id")
        return await self.fetchall(
            "SELECT * FROM github_repositories WHERE owner_id = ? ORDER BY id", (owner_id,)
        )

    async def delete_github_repository(self, owner_id: str, repo_owner: str, repo_name: str) -> bool:
        where = (owner_id, repo_owner, repo_name)
        await self.conn.execute(
            "DELETE FROM github_snapshots WHERE repo_id IN "
            "(SELECT id FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?)",
            where,
        )
        await self.conn.execute(
            "DELETE FROM github_notifications WHERE repo_id IN "
            "(SELECT id FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?)",
            where,
        )
        async with self.conn.execute(
            "DELETE FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
            where,
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount > 0

    async def insert_github_snapshot(self, repo_id: int, snapshot: dict) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO github_snapshots "
            "(repo_id, stars, forks, watchers, commits_count, latest_commit_sha, latest_commit_message, "
            "latest_commit_author, latest_commit_time, latest_release, open_issues_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id,
                snapshot["stars"],
                snapshot["forks"],
                snapshot["watchers"],
                snapshot["commits_count"],
                snapshot["latest_commit_sha"],
                snapshot["latest_commit_message"],
                snapshot["latest_commit_author"],
                snapshot["latest_commit_time"],
                snapshot["latest_release"],
                snapshot["open_issues_count"],
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def fetch_latest_github_snapshot(self, repo_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_snapshots WHERE repo_id = ? ORDER BY id DESC LIMIT 1", (repo_id,)
        )

    async def upsert_github_notification(
        self, repo_id: int, target_type: str, target_id: str, enabled: bool = True
    ) -> None:
        await self.execute(
            "INSERT INTO github_notifications (repo_id, target_type, target_id, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo_id, target_type, target_id) DO UPDATE SET enabled = excluded.enabled",
            (repo_id, target_type, target_id, int(enabled)),
        )

    async def fetch_github_notifications(self, repo_id: int) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM github_notifications WHERE repo_id = ? AND enabled = 1 ORDER BY target_type, target_id",
            (repo_id,),
        )

    async def fetch_github_snapshots_between(self, repo_id: int, period_start: str, period_end: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM github_snapshots WHERE repo_id = ? AND datetime(captured_at) >= datetime(?) "
            "AND datetime(captured_at) < datetime(?) ORDER BY id",
            (repo_id, period_start, period_end),
        )

    async def fetch_latest_github_snapshot_before(self, repo_id: int, period_start: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_snapshots WHERE repo_id = ? AND datetime(captured_at) < datetime(?) "
            "ORDER BY id DESC LIMIT 1",
            (repo_id, period_start),
        )

    async def fetch_scheduled_tasks_for_report(
        self, owner_id: str, period_start: str, period_end: str
    ) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM scheduled_tasks WHERE owner_id = ? AND "
            "((datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)) "
            "OR (datetime(last_run) >= datetime(?) AND datetime(last_run) < datetime(?)) "
            "OR (datetime(next_run) >= datetime(?) AND datetime(next_run) < datetime(?))) ORDER BY id",
            (owner_id, period_start, period_end, period_start, period_end, period_start, period_end),
        )

    async def fetch_memories_for_report(self, user_id: str, period_start: str, period_end: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM memories WHERE user_id = ? AND datetime(updated_at) >= datetime(?) "
            "AND datetime(updated_at) < datetime(?) "
            "AND importance >= 0.8 ORDER BY importance DESC, id",
            (user_id, period_start, period_end),
        )

    async def fetch_daily_report_users(self) -> list[str]:
        rows = await self.fetchall("SELECT user_id FROM notification_settings WHERE daily_report = 1 ORDER BY user_id")
        return [str(row["user_id"]) for row in rows]

    async def save_report(
        self, owner_id: str, report_type: str, period_start: str, period_end: str, content: str
    ) -> int:
        await self.execute(
            "INSERT INTO reports (owner_id, report_type, period_start, period_end, content) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, report_type, period_start) DO UPDATE SET period_end = excluded.period_end, content = excluded.content",
            (owner_id, report_type, period_start, period_end, content),
        )
        row = await self.fetchone(
            "SELECT id FROM reports WHERE owner_id = ? AND report_type = ? AND period_start = ?",
            (owner_id, report_type, period_start),
        )
        return int(row["id"])

    # ---------- pending_broadcasts ----------

    async def create_pending_broadcast(self, admin_id: str, targets: list[dict], content: str, ttl_seconds: int) -> int:
        from datetime import datetime, timedelta

        # 同一管理员只保留最新一条待确认任务，旧的自动作废
        await self.execute(
            "UPDATE pending_broadcasts SET status = ? WHERE admin_id = ? AND status = ?",
            (_PENDING_CANCELLED, admin_id, _PENDING_ACTIVE),
        )
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "INSERT INTO pending_broadcasts (admin_id, targets_json, content, status, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (admin_id, json.dumps(targets, ensure_ascii=False), content, _PENDING_ACTIVE, expires_at),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def get_active_pending(self, admin_id: str) -> dict | None:
        """取该管理员最新的 active pending；已过期的顺手标记为 expired。"""
        row = await self.fetchone(
            "SELECT * FROM pending_broadcasts WHERE admin_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (admin_id, _PENDING_ACTIVE),
        )
        if row is None:
            return None
        if row["expires_at"] <= _now():
            await self.execute(
                "UPDATE pending_broadcasts SET status = ? WHERE id = ? AND status = ?",
                (_PENDING_EXPIRED, row["id"], _PENDING_ACTIVE),
            )
            return None
        row["targets"] = json.loads(row["targets_json"])
        return row

    async def finish_pending(self, pending_id: int, status: str) -> bool:
        """把 active 置为终态。返回 False 说明已被处理（防重复执行）。"""
        async with self.conn.execute(
            "UPDATE pending_broadcasts SET status = ? WHERE id = ? AND status = ?",
            (status, pending_id, _PENDING_ACTIVE),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount > 0
