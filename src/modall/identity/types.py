"""Framework-independent identity and authorization types."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW_RESOURCES = "view_resources"
    VIEW_AUDIT = "view_audit"
    SEARCH_REGISTRY = "search_registry"
    MANAGE_CONNECTION_CONFIGURATION = "manage_connection_configuration"
    VERIFY_CONNECTION = "verify_connection"
    MANAGE_CAPABILITY = "manage_capability"
    INVOKE = "invoke"
    DISABLE_CONNECTION = "disable_connection"
    ENABLE_CONNECTION = "enable_connection"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.OPERATOR: frozenset(
        {
            Permission.VIEW_RESOURCES,
            Permission.VIEW_AUDIT,
            Permission.SEARCH_REGISTRY,
            Permission.VERIFY_CONNECTION,
            Permission.MANAGE_CAPABILITY,
            Permission.INVOKE,
            Permission.DISABLE_CONNECTION,
        }
    ),
    Role.VIEWER: frozenset({Permission.VIEW_RESOURCES}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    issuer: str
    subject: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    workspace_id: UUID
    actor_user_id: UUID
    role: Role

    def allows(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]
