from app.security.permissions import PermissionService


def test_admin_allowed():
    svc = PermissionService(["123456789", "87654321"])
    assert svc.is_admin("123456789") is True
    assert svc.is_admin(87654321) is True  # 数字自动转 str


def test_normal_user_denied():
    svc = PermissionService(["123456789"])
    assert svc.is_admin("11111111") is False
    assert svc.is_admin("") is False


def test_empty_admin_list():
    svc = PermissionService([])
    assert svc.is_admin("123456789") is False
