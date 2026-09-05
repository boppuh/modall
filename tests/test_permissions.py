import pytest

from modall.identity.repository import AuthorizationDenied, require_role
from modall.identity.types import Permission, Role, WorkspaceContext


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (Role.ADMIN, Permission.ENABLE_CONNECTION, True),
        (Role.OPERATOR, Permission.DISABLE_CONNECTION, True),
        (Role.OPERATOR, Permission.ENABLE_CONNECTION, False),
        (Role.OPERATOR, Permission.MANAGE_CONNECTION_CONFIGURATION, False),
        (Role.VIEWER, Permission.VIEW_RESOURCES, True),
        (Role.VIEWER, Permission.VIEW_AUDIT, False),
    ],
)
def test_permission_matrix(role: Role, permission: Permission, allowed: bool) -> None:
    from uuid import uuid4

    context = WorkspaceContext(workspace_id=uuid4(), actor_user_id=uuid4(), role=role)

    assert context.allows(permission) is allowed


def test_require_role_fails_closed() -> None:
    from uuid import uuid4

    context = WorkspaceContext(workspace_id=uuid4(), actor_user_id=uuid4(), role=Role.OPERATOR)

    with pytest.raises(AuthorizationDenied, match="workspace access denied"):
        require_role(context, Role.ADMIN)
