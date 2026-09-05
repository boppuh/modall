"""Workspace-scoped repositories and current-membership authorization."""

from typing import cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.identity.types import Permission, Role, WorkspaceContext
from modall.persistence.models import AuditEvent, SecretBinding, Workspace, WorkspaceMembership


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
        if not self.context.allows(Permission.VIEW_AUDIT):
            raise AuthorizationDenied("workspace access denied")
        statement = (
            select(AuditEvent)
            .where(AuditEvent.workspace_id == self.context.workspace_id)
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        )
        return list((await self._session.scalars(statement)).all())


def require_role(context: WorkspaceContext, *roles: Role) -> None:
    if context.role not in roles:
        raise AuthorizationDenied("workspace access denied")


async def require_current_role(
    session: AsyncSession,
    context: WorkspaceContext,
    *roles: Role,
    serialize_workspace: bool = False,
) -> WorkspaceMembership:
    """Revalidate current membership, optionally serialized with workspace mutations."""

    require_role(context, *roles)
    if serialize_workspace:
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.id == context.workspace_id).with_for_update()
        )
        if workspace_id is None:
            raise AuthorizationDenied("workspace access denied")
    membership = await session.scalar(
        select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == context.workspace_id,
            WorkspaceMembership.user_id == context.actor_user_id,
        )
        .execution_options(populate_existing=True)
    )
    if membership is None or membership.typed_role not in roles:
        raise AuthorizationDenied("workspace access denied")
    return membership
