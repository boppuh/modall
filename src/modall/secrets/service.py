"""Secret-binding metadata operations; secret values never enter persistence."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.persistence.models import AuditEvent, SecretBinding
from modall.secrets.provider import (
    SecretProviderError,
    SecretReference,
    validate_secret_reference,
)


class SecretBindingService:
    def __init__(self, session: AsyncSession, *, active_provider: str) -> None:
        if active_provider not in {"fixture", "mounted_file"}:
            raise ValueError("unsupported active secret provider")
        self._session = session
        self._active_provider = active_provider

    async def create_binding(
        self,
        *,
        context: WorkspaceContext,
        reference: SecretReference,
        correlation_id: UUID | None = None,
    ) -> SecretBinding:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            serialize_workspace=True,
        )
        if reference.provider != self._active_provider:
            raise SecretProviderError("secret provider does not match active provider")
        validate_secret_reference(reference)
        binding = SecretBinding(
            workspace_id=context.workspace_id,
            provider=reference.provider,
            external_reference=reference.external_reference,
            version=reference.version,
            created_by_user_id=context.actor_user_id,
        )
        self._session.add(binding)
        await self._session.flush()
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=AuditAction.SECRET_BINDING_CREATED,
                resource_type=ResourceType.SECRET_BINDING,
                resource_id=binding.id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        return binding
