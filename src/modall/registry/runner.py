"""Refresh worker orchestration without retaining credentials or upstream errors."""

from collections.abc import Callable
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.identity.repository import AuthorizationDenied, require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.mcp_adapter.client import (
    CredentialError,
    DiscoveryError,
    McpClientAdapter,
    ProtocolMismatch,
)
from modall.mcp_adapter.policy import (
    EndpointPolicyError,
    EndpointResolutionError,
    ResponseLimitExceeded,
)
from modall.persistence.database import transaction
from modall.persistence.models import SecretBinding, ServerConnectionVersion
from modall.registry.discovery import (
    DiscoveryPublicationService,
    RefreshJobService,
    RefreshLease,
)
from modall.registry.service import InvalidConnectionTransition
from modall.registry.types import DiscoveryFailureCode
from modall.secrets.provider import SecretProvider, SecretProviderError, SecretReference

AdapterFactory = Callable[[str], McpClientAdapter]


class DiscoveryRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        secret_provider: SecretProvider,
        adapter_factory: AdapterFactory,
    ) -> None:
        self._session_factory = session_factory
        self._secret_provider = secret_provider
        self._adapter_factory = adapter_factory

    async def run(self, *, context: WorkspaceContext, lease: RefreshLease) -> UUID | None:
        try:
            endpoint, policy_version, secret_reference = await self._load_target(context, lease)
            async with transaction(self._session_factory) as session:
                await RefreshJobService(session).validate_for_contact(context=context, lease=lease)
            adapter = self._adapter_factory(policy_version)
            if secret_reference is None:
                result = await adapter.discover(endpoint)
            else:
                with self._secret_provider.retrieve(secret_reference) as credential:
                    result = await adapter.discover(endpoint, bearer_token=credential)
        except (AuthorizationDenied, InvalidConnectionTransition):
            return None
        except (
            DiscoveryError,
            EndpointPolicyError,
            EndpointResolutionError,
            ResponseLimitExceeded,
            SecretProviderError,
            TimeoutError,
            httpx.HTTPError,
        ) as error:
            code = classify_discovery_failure(error)
            try:
                async with transaction(self._session_factory) as session:
                    await DiscoveryPublicationService(session).publish_failure(
                        context=context,
                        lease=lease,
                        error_code=code,
                    )
            except (AuthorizationDenied, InvalidConnectionTransition):
                return None
            return None
        try:
            async with transaction(self._session_factory) as session:
                snapshot = await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result,
                )
                return snapshot.id
        except (AuthorizationDenied, InvalidConnectionTransition):
            return None

    async def _load_target(
        self, context: WorkspaceContext, lease: RefreshLease
    ) -> tuple[str, str, SecretReference | None]:
        async with transaction(self._session_factory) as session:
            await require_current_role(session, context, Role.ADMIN, Role.OPERATOR)
            version = await session.scalar(
                select(ServerConnectionVersion).where(
                    ServerConnectionVersion.id == lease.connection_version_id,
                    ServerConnectionVersion.connection_id == lease.connection_id,
                    ServerConnectionVersion.workspace_id == context.workspace_id,
                )
            )
            if version is None:
                raise AuthorizationDenied("workspace access denied")
            if version.secret_binding_id is None:
                return version.endpoint_url, version.policy_version, None
            binding = await session.scalar(
                select(SecretBinding).where(
                    SecretBinding.id == version.secret_binding_id,
                    SecretBinding.workspace_id == context.workspace_id,
                )
            )
            if binding is None:
                raise SecretProviderError("secret binding is unavailable")
            return (
                version.endpoint_url,
                version.policy_version,
                SecretReference(
                    provider=binding.provider,
                    external_reference=binding.external_reference,
                    version=binding.version,
                ),
            )


def classify_discovery_failure(error: BaseException) -> DiscoveryFailureCode:
    errors = _flatten(error)
    if any(isinstance(item, ProtocolMismatch) for item in errors):
        return DiscoveryFailureCode.PROTOCOL_MISMATCH
    if any(isinstance(item, CredentialError) for item in errors):
        return DiscoveryFailureCode.AUTHENTICATION_FAILED
    if any(isinstance(item, EndpointResolutionError) for item in errors):
        return DiscoveryFailureCode.TRANSPORT_FAILED
    if any(isinstance(item, EndpointPolicyError) for item in errors):
        return DiscoveryFailureCode.ENDPOINT_REJECTED
    if any(isinstance(item, ResponseLimitExceeded) for item in errors):
        return DiscoveryFailureCode.RESPONSE_LIMIT
    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in errors):
        return DiscoveryFailureCode.TIMEOUT
    if any(
        isinstance(item, httpx.HTTPStatusError) and item.response.status_code == 401
        for item in errors
    ):
        return DiscoveryFailureCode.AUTHENTICATION_FAILED
    if any(isinstance(item, SecretProviderError) for item in errors):
        return DiscoveryFailureCode.AUTHENTICATION_FAILED
    if any(isinstance(item, httpx.HTTPError) for item in errors):
        return DiscoveryFailureCode.TRANSPORT_FAILED
    if any(isinstance(item, DiscoveryError) for item in errors):
        return DiscoveryFailureCode.INVALID_METADATA
    return DiscoveryFailureCode.TRANSPORT_FAILED


def _flatten(error: BaseException) -> list[BaseException]:
    flattened = [error]
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            flattened.extend(_flatten(child))
    if error.__cause__ is not None:
        flattened.extend(_flatten(error.__cause__))
    return flattened
