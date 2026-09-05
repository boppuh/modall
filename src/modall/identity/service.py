"""Identity bootstrap operations with atomic audit evidence."""

from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import AuthorizationDenied, require_role
from modall.identity.types import Principal, Role, WorkspaceContext
from modall.persistence.models import AuditEvent, User, Workspace, WorkspaceMembership


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_user(self, principal: Principal) -> User:
        statement = select(User).where(
            User.oidc_issuer == principal.issuer,
            User.oidc_subject == principal.subject,
        )
        user = await self._session.scalar(statement)
        if user is not None:
            return user
        try:
            async with self._session.begin_nested():
                user = User(
                    oidc_issuer=principal.issuer,
                    oidc_subject=principal.subject,
                    display_name=principal.display_name,
                )
                self._session.add(user)
                await self._session.flush()
            return user
        except IntegrityError:
            # A concurrent first login may have inserted the same principal while
            # this transaction waited on the unique constraint. The savepoint keeps
            # the caller's transaction usable so the winning row can be reloaded.
            winner = cast(User | None, await self._session.scalar(statement))
            if winner is None:
                raise
            return winner

    async def create_workspace(
        self,
        *,
        owner: User,
        name: str,
        correlation_id: UUID | None = None,
    ) -> Workspace:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("workspace name must not be blank")
        workspace = Workspace(name=normalized_name)
        self._session.add(workspace)
        await self._session.flush()
        self._session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=owner.id,
                role=Role.ADMIN.value,
            )
        )
        await self._session.flush()
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=workspace.id,
                actor_user_id=owner.id,
                action=AuditAction.WORKSPACE_CREATED,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        return workspace

    async def set_membership_role(
        self,
        *,
        context: WorkspaceContext,
        user_id: UUID,
        role: Role,
        correlation_id: UUID | None = None,
    ) -> WorkspaceMembership:
        """Add or change a member while preserving at least one workspace Admin."""

        require_role(context, Role.ADMIN)
        locked_workspace = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == context.workspace_id).with_for_update()
        )
        if locked_workspace is None:
            raise AuthorizationDenied("workspace access denied")

        actor_membership = await self._session.scalar(
            select(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == context.workspace_id,
                WorkspaceMembership.user_id == context.actor_user_id,
            )
            .execution_options(populate_existing=True)
        )
        if actor_membership is None or actor_membership.typed_role != Role.ADMIN:
            raise AuthorizationDenied("workspace access denied")

        membership = await self._session.scalar(
            select(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == context.workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        if membership is None:
            membership = WorkspaceMembership(
                workspace_id=context.workspace_id,
                user_id=user_id,
                role=role.value,
            )
            self._session.add(membership)
        elif membership.role == Role.ADMIN.value and role != Role.ADMIN:
            admin_count = await self._session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == context.workspace_id,
                    WorkspaceMembership.role == Role.ADMIN.value,
                )
            )
            if admin_count is None or admin_count <= 1:
                raise AuthorizationDenied("workspace must retain an admin")
            membership.role = role.value
        else:
            membership.role = role.value
        await self._session.flush()
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=AuditAction.MEMBERSHIP_CHANGED,
                resource_type=ResourceType.MEMBERSHIP,
                resource_id=user_id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        return membership
