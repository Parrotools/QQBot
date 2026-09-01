"""会话管理：session key 规则 + 上下文读写 + 截断。

session key 规则：
- 私聊：private:{user_id}
- 群聊（默认，成员独立上下文）：group:{group_id}:{user_id}
- 群聊（GROUP_SHARED_CONTEXT=true，全群共享）：group:{group_id}
"""

from app.database.db import Database


class SessionManager:
    def __init__(self, db: Database, max_context_messages: int = 20):
        self._db = db
        self._max = max(2, max_context_messages)

    @staticmethod
    def private_key(user_id: str) -> str:
        return f"private:{user_id}"

    @staticmethod
    def group_key(group_id: str, user_id: str, shared: bool = False) -> str:
        if shared:
            return f"group:{group_id}"
        return f"group:{group_id}:{user_id}"

    async def get_context(self, session_key: str) -> list[dict]:
        """返回按时间升序（旧→新）的最近 N 条 {role, content}。"""
        rows = await self._db.fetchall(
            "SELECT role, content FROM ("
            "  SELECT id, role, content FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (session_key, self._max),
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def append(self, session_key: str, role: str, content: str) -> None:
        await self._db.execute(
            "INSERT INTO messages (session_key, role, content) VALUES (?, ?, ?)",
            (session_key, role, content),
        )
        await self._db.execute(
            "INSERT INTO sessions (session_key) VALUES (?) "
            "ON CONFLICT(session_key) DO UPDATE SET updated_at = datetime('now')",
            (session_key,),
        )
        # 只保留最近 N 条，防止上下文无限增长
        await self._db.execute(
            "DELETE FROM messages WHERE session_key = ? AND id NOT IN ("
            "  SELECT id FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ")",
            (session_key, session_key, self._max),
        )

    async def clear(self, session_key: str) -> None:
        await self._db.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
        await self._db.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
