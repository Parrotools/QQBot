"""aiosqlite 封装。

选型说明：第一版只有 4 张表、单实例、低并发，SQLAlchemy async 属于过度设计；
这里用 aiosqlite + 本类提供的少量 repository 方法，业务层不直接写 SQL，
未来需要换 PostgreSQL/ORM 时只需替换本层。
"""

import json
from datetime import UTC
from pathlib import Path

import aiosqlite

from app.database.models import SCHEMA

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
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

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
