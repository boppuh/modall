"""Workspace-scoped repositories and current-membership authorization."""

from typing import cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.identity.types import Permission, Role, WorkspaceContext
from modall.persistence.models import AuditEvent, SecretBinding, WorkspaceMembership


class AuthorizationDenied(Exception):
    """Fail-closed authorization result without resource-existence disclosure."""


class AuthorizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorize(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        permission: Permission,
    ) -> WorkspaceContext:
        membership = await self._session.get(
            WorkspaceMembership,
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        if membership is None:
            raise AuthorizationDenied("workspace access denied")
        context = WorkspaceContext(
            workspace_id=workspace_id,
            actor_user_id=user_id,
            role=membership.typed_role,
        )
        if not context.allows(permission):
            raise AuthorizationDenied("workspace access denied")
        return context


class WorkspaceRepository:
    """Repository whose queries always carry an immutable workspace scope."""

    def __init__(self, session: AsyncSession, context: WorkspaceContext) -> None:
        self._session = session
        self.context = context

    def _scoped(self, statement: Select[tuple[SecretBinding]]) -> Select[tuple[SecretBinding]]:
        return statement.where(SecretBinding.workspace_id == self.context.workspace_id)

    async def get_secret_binding(self, binding_id: UUID) -> SecretBinding | None:
        statement = self._scoped(select(SecretBinding).where(SecretBinding.id == binding_id))
        return cast(SecretBinding | None, await self._session.scalar(statement))

    async def list_audit_events(self) -> list[AuditEvent]:
        statement = (
            select(AuditEvent)
            .where(AuditEvent.workspace_id == self.context.workspace_id)
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        )
        return list((await self._session.scalars(statement)).all())


def require_role(context: WorkspaceContext, *roles: Role) -> None:
    if context.role not in roles:
        raise AuthorizationDenied("workspace access denied")
