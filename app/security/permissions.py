"""权限服务：管理员判定。普通聊天/网页总结人人可用，发送类操作仅管理员。"""

from collections.abc import Iterable


class PermissionService:
    def __init__(self, admin_ids: Iterable[str]):
        self._admin_ids = frozenset(str(x) for x in admin_ids)

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self._admin_ids

    @property
    def admin_ids(self) -> frozenset[str]:
        return self._admin_ids
