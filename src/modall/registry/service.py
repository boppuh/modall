"""Workspace-scoped connection versioning and lifecycle operations."""

import re
from ipaddress import ip_address
from socket import inet_aton
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import AuthorizationDenied, require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.persistence.models import (
    AuditEvent,
    Capability,
    CapabilityStatusEvent,
    CapabilityVersion,
    McpToolBinding,
    SecretBinding,
    ServerConnection,
    ServerConnectionVersion,
)
from modall.registry.types import CapabilityStatus, ConnectionLifecycle, Transport


class InvalidConnectionTransition(Exception):
    """A safe rejection of an invalid or stale lifecycle transition."""


class InvalidCapabilityTransition(Exception):
    """A safe rejection of an invalid or stale capability transition."""


class ConnectionService:
    _POLICY_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        context: WorkspaceContext,
        name: str,
        endpoint_url: str,
        secret_binding_id: UUID | None,
        policy_version: str,
        correlation_id: UUID | None = None,
    ) -> ServerConnection:
        await require_current_role(self._session, context, Role.ADMIN, serialize_workspace=True)
        normalized_name = self._validate_name(name)
        self._validate_configuration(endpoint_url, policy_version)
        await self._require_scoped_secret(context, secret_binding_id)

        connection_id = uuid4()
        version_id = uuid4()
        connection = ServerConnection(
            id=connection_id,
            workspace_id=context.workspace_id,
            name=normalized_name,
            lifecycle=ConnectionLifecycle.VERIFYING.value,
            pending_version_id=version_id,
            verified_version_id=None,
            control_epoch=0,
            refresh_generation=0,
            allocated_control_epoch=None,
            allocated_target_version_id=None,
            created_by_user_id=context.actor_user_id,
        )
        version = ServerConnectionVersion(
            id=version_id,
            workspace_id=context.workspace_id,
            connection_id=connection_id,
            sequence=1,
            endpoint_url=endpoint_url,
            secret_binding_id=secret_binding_id,
            transport=Transport.STREAMABLE_HTTP.value,
            policy_version=policy_version,
            created_by_user_id=context.actor_user_id,
        )
        self._session.add_all((connection, version))
        await self._session.flush()
        self._audit(context, AuditAction.CONNECTION_CREATED, connection.id, correlation_id)
        await self._session.flush()
        return connection

    async def append_version(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        endpoint_url: str,
        secret_binding_id: UUID | None,
        policy_version: str,
        correlation_id: UUID | None = None,
    ) -> ServerConnectionVersion:
        await require_current_role(self._session, context, Role.ADMIN, serialize_workspace=True)
        self._validate_configuration(endpoint_url, policy_version)
        await self._require_scoped_secret(context, secret_binding_id)
        connection = await self._locked_connection(context, connection_id)
        if connection.typed_lifecycle == ConnectionLifecycle.DISABLED:
            raise InvalidConnectionTransition("connection transition rejected")
        sequence = await self._session.scalar(
            select(func.max(ServerConnectionVersion.sequence)).where(
                ServerConnectionVersion.connection_id == connection.id
            )
        )
        version = ServerConnectionVersion(
            workspace_id=context.workspace_id,
            connection_id=connection.id,
            sequence=(sequence or 0) + 1,
            endpoint_url=endpoint_url,
            secret_binding_id=secret_binding_id,
            transport=Transport.STREAMABLE_HTTP.value,
            policy_version=policy_version,
            created_by_user_id=context.actor_user_id,
        )
        self._session.add(version)
        await self._session.flush()
        connection.pending_version_id = version.id
        connection.lifecycle = ConnectionLifecycle.VERIFYING.value
        connection.control_epoch += 1
        self._audit(context, AuditAction.CONNECTION_VERSION_APPENDED, connection.id, correlation_id)
        await self._session.flush()
        return version

    async def promote_pending(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        expected_version_id: UUID,
        expected_control_epoch: int,
        expected_refresh_generation: int,
        correlation_id: UUID | None = None,
    ) -> ServerConnection:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        connection = await self._locked_connection(context, connection_id)
        current_target = connection.pending_version_id or connection.verified_version_id
        if (
            connection.typed_lifecycle != ConnectionLifecycle.VERIFYING
            or current_target != expected_version_id
            or connection.control_epoch != expected_control_epoch
            or connection.refresh_generation != expected_refresh_generation
            or connection.allocated_control_epoch != expected_control_epoch
            or connection.allocated_target_version_id != expected_version_id
        ):
            raise InvalidConnectionTransition("connection transition rejected")
        connection.verified_version_id = expected_version_id
        connection.pending_version_id = None
        connection.lifecycle = ConnectionLifecycle.ACTIVE.value
        self._audit(context, AuditAction.CONNECTION_VERIFIED, connection.id, correlation_id)
        await self._session.flush()
        return connection

    async def disable(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        correlation_id: UUID | None = None,
    ) -> ServerConnection:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        connection = await self._locked_connection(context, connection_id)
        if connection.typed_lifecycle == ConnectionLifecycle.DISABLED:
            raise InvalidConnectionTransition("connection transition rejected")
        connection.lifecycle = ConnectionLifecycle.DISABLED.value
        connection.control_epoch += 1
        self._audit(context, AuditAction.CONNECTION_DISABLED, connection.id, correlation_id)
        await self._session.flush()
        return connection

    async def enable(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        correlation_id: UUID | None = None,
    ) -> ServerConnection:
        await require_current_role(self._session, context, Role.ADMIN, serialize_workspace=True)
        connection = await self._locked_connection(context, connection_id)
        if connection.typed_lifecycle != ConnectionLifecycle.DISABLED:
            raise InvalidConnectionTransition("connection transition rejected")
        connection.lifecycle = ConnectionLifecycle.VERIFYING.value
        connection.control_epoch += 1
        self._audit(context, AuditAction.CONNECTION_ENABLED, connection.id, correlation_id)
        await self._session.flush()
        return connection

    async def allocate_refresh_generation(
        self, *, context: WorkspaceContext, connection_id: UUID
    ) -> tuple[int, int, UUID]:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        connection = await self._locked_connection(context, connection_id)
        if connection.typed_lifecycle == ConnectionLifecycle.DISABLED:
            raise InvalidConnectionTransition("connection transition rejected")
        target = connection.pending_version_id or connection.verified_version_id
        if target is None:
            raise InvalidConnectionTransition("connection transition rejected")
        connection.refresh_generation += 1
        connection.allocated_control_epoch = connection.control_epoch
        connection.allocated_target_version_id = target
        await self._session.flush()
        return connection.refresh_generation, connection.control_epoch, target

    @staticmethod
    def is_executable(connection: ServerConnection, connection_version_id: UUID) -> bool:
        return (
            connection.typed_lifecycle == ConnectionLifecycle.ACTIVE
            and connection.pending_version_id is None
            and connection.verified_version_id == connection_version_id
        )

    async def _locked_connection(
        self, context: WorkspaceContext, connection_id: UUID
    ) -> ServerConnection:
        connection = await self._session.scalar(
            select(ServerConnection)
            .where(
                ServerConnection.id == connection_id,
                ServerConnection.workspace_id == context.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if connection is None:
            raise AuthorizationDenied("workspace access denied")
        return connection

    async def _require_scoped_secret(
        self, context: WorkspaceContext, secret_binding_id: UUID | None
    ) -> None:
        if secret_binding_id is None:
            return
        binding = await self._session.scalar(
            select(SecretBinding.id).where(
                SecretBinding.id == secret_binding_id,
                SecretBinding.workspace_id == context.workspace_id,
            )
        )
        if binding is None:
            raise AuthorizationDenied("workspace access denied")

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = name.strip()
        if not normalized or len(normalized) > 128 or "\x00" in normalized:
            raise ValueError("connection name must contain between 1 and 128 characters")
        return normalized

    @classmethod
    def _validate_configuration(cls, endpoint_url: str, policy_version: str) -> None:
        if not endpoint_url or len(endpoint_url) > 2048 or "\x00" in endpoint_url:
            raise ValueError("invalid endpoint URL")
        try:
            parsed = urlsplit(endpoint_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid endpoint URL") from exc
        unicode_hostname = parsed.hostname.rstrip(".").lower() if parsed.hostname else ""
        try:
            hostname = unicode_hostname.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError as exc:
            raise ValueError("invalid endpoint URL") from exc
        try:
            ip_address(hostname)
        except ValueError:
            try:
                inet_aton(hostname)
            except OSError:
                is_ip_literal = False
            else:
                is_ip_literal = True
        else:
            is_ip_literal = True
        if (
            parsed.scheme != "https"
            or not hostname
            or hostname == "localhost"
            or hostname.endswith(".localhost")
            or is_ip_literal
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise ValueError("invalid endpoint URL")
        if cls._POLICY_VERSION.fullmatch(policy_version) is None:
            raise ValueError("invalid policy version")

    def _audit(
        self,
        context: WorkspaceContext,
        action: AuditAction,
        connection_id: UUID,
        correlation_id: UUID | None,
    ) -> None:
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=action,
                resource_type=ResourceType.SERVER_CONNECTION,
                resource_id=connection_id,
                correlation_id=correlation_id or uuid4(),
            )
        )


class CapabilityService:
    """Persist immutable tool metadata and monotonic enablement projections."""

    _DIGEST = re.compile(r"[0-9a-f]{64}\Z")
    _PROTOCOL_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_version(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        connection_version_id: UUID,
        expected_control_epoch: int,
        expected_refresh_generation: int,
        tool_identity: str,
        tool_name: str,
        display_name: str,
        description: str | None,
        input_schema: dict[str, object],
        output_schema: dict[str, object] | None,
        metadata_digest: str,
        protocol_revision: str,
        correlation_id: UUID | None = None,
    ) -> CapabilityVersion:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        self._validate_metadata(
            tool_identity=tool_identity,
            tool_name=tool_name,
            display_name=display_name,
            description=description,
            metadata_digest=metadata_digest,
            protocol_revision=protocol_revision,
        )
        connection = await self._session.scalar(
            select(ServerConnection)
            .where(
                ServerConnection.id == connection_id,
                ServerConnection.workspace_id == context.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if connection is None:
            raise AuthorizationDenied("workspace access denied")
        current_target = connection.pending_version_id or connection.verified_version_id
        if (
            connection.typed_lifecycle == ConnectionLifecycle.DISABLED
            or current_target != connection_version_id
            or connection.control_epoch != expected_control_epoch
            or connection.refresh_generation != expected_refresh_generation
            or connection.allocated_control_epoch != expected_control_epoch
            or connection.allocated_target_version_id != connection_version_id
        ):
            raise InvalidConnectionTransition("connection transition rejected")

        capability = await self._session.scalar(
            select(Capability)
            .where(
                Capability.workspace_id == context.workspace_id,
                Capability.connection_id == connection_id,
                Capability.tool_identity == tool_identity,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if capability is not None:
            existing = await self._matching_current_version(
                capability=capability,
                connection_version_id=connection_version_id,
                tool_name=tool_name,
                display_name=display_name,
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                metadata_digest=metadata_digest,
                protocol_revision=protocol_revision,
            )
            if existing is not None:
                return existing
            sequence = (
                await self._session.scalar(
                    select(func.max(CapabilityVersion.sequence)).where(
                        CapabilityVersion.capability_id == capability.id
                    )
                )
                or 0
            ) + 1
            version_id = uuid4()
        else:
            capability = Capability(
                id=uuid4(),
                workspace_id=context.workspace_id,
                connection_id=connection_id,
                tool_identity=tool_identity,
                pending_version_id=None,
                enabled_version_id=None,
                status=CapabilityStatus.PENDING_REVIEW.value,
                status_epoch=0,
            )
            self._session.add(capability)
            sequence = 1
            version_id = uuid4()

        version = CapabilityVersion(
            id=version_id,
            workspace_id=context.workspace_id,
            capability_id=capability.id,
            sequence=sequence,
            display_name=display_name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            metadata_digest=metadata_digest,
        )
        binding = McpToolBinding(
            capability_version_id=version_id,
            capability_id=capability.id,
            workspace_id=context.workspace_id,
            connection_id=connection_id,
            connection_version_id=connection_version_id,
            tool_name=tool_name,
            protocol_revision=protocol_revision,
        )
        self._session.add_all((version, binding))
        capability.pending_version_id = version_id
        capability.status = CapabilityStatus.PENDING_REVIEW.value
        capability.status_epoch += 1
        await self._session.flush()
        self._append_status_event(context, capability, version_id)
        self._audit(
            context,
            AuditAction.CAPABILITY_VERSION_RECORDED,
            capability.id,
            correlation_id,
        )
        await self._session.flush()
        return version

    async def enable(
        self,
        *,
        context: WorkspaceContext,
        capability_id: UUID,
        expected_version_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Capability:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        capability = await self._locked_capability(context, capability_id)
        allowed_version = capability.pending_version_id or capability.enabled_version_id
        if (
            allowed_version != expected_version_id
            or capability.typed_status == CapabilityStatus.ENABLED
        ):
            raise InvalidCapabilityTransition("capability transition rejected")
        binding = await self._session.scalar(
            select(McpToolBinding).where(
                McpToolBinding.capability_version_id == expected_version_id,
                McpToolBinding.capability_id == capability.id,
                McpToolBinding.workspace_id == context.workspace_id,
            )
        )
        connection = await self._session.scalar(
            select(ServerConnection)
            .where(
                ServerConnection.id == capability.connection_id,
                ServerConnection.workspace_id == context.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            binding is None
            or connection is None
            or connection.typed_lifecycle == ConnectionLifecycle.DISABLED
            or (connection.pending_version_id or connection.verified_version_id)
            != binding.connection_version_id
        ):
            raise InvalidCapabilityTransition("capability transition rejected")
        capability.enabled_version_id = expected_version_id
        capability.pending_version_id = None
        capability.status = CapabilityStatus.ENABLED.value
        capability.status_epoch += 1
        self._append_status_event(context, capability, expected_version_id)
        self._audit(context, AuditAction.CAPABILITY_ENABLED, capability.id, correlation_id)
        await self._session.flush()
        return capability

    async def disable(
        self,
        *,
        context: WorkspaceContext,
        capability_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Capability:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        capability = await self._locked_capability(context, capability_id)
        if capability.typed_status == CapabilityStatus.DISABLED:
            raise InvalidCapabilityTransition("capability transition rejected")
        capability.status = CapabilityStatus.DISABLED.value
        capability.status_epoch += 1
        self._append_status_event(context, capability, capability.enabled_version_id)
        self._audit(context, AuditAction.CAPABILITY_DISABLED, capability.id, correlation_id)
        await self._session.flush()
        return capability

    async def _matching_current_version(
        self,
        *,
        capability: Capability,
        connection_version_id: UUID,
        tool_name: str,
        display_name: str,
        description: str | None,
        input_schema: dict[str, object],
        output_schema: dict[str, object] | None,
        metadata_digest: str,
        protocol_revision: str,
    ) -> CapabilityVersion | None:
        current_id = capability.pending_version_id or capability.enabled_version_id
        if current_id is None:
            return None
        row = await self._session.execute(
            select(CapabilityVersion, McpToolBinding)
            .join(
                McpToolBinding,
                McpToolBinding.capability_version_id == CapabilityVersion.id,
            )
            .where(
                CapabilityVersion.id == current_id,
                CapabilityVersion.capability_id == capability.id,
                CapabilityVersion.workspace_id == capability.workspace_id,
            )
        )
        found = row.one_or_none()
        if found is None:
            return None
        version, binding = found
        if (
            version.display_name == display_name
            and version.description == description
            and version.input_schema == input_schema
            and version.output_schema == output_schema
            and version.metadata_digest == metadata_digest
            and binding.connection_version_id == connection_version_id
            and binding.tool_name == tool_name
            and binding.protocol_revision == protocol_revision
        ):
            return cast(CapabilityVersion, version)
        return None

    async def _locked_capability(
        self, context: WorkspaceContext, capability_id: UUID
    ) -> Capability:
        capability = await self._session.scalar(
            select(Capability)
            .where(
                Capability.id == capability_id,
                Capability.workspace_id == context.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if capability is None:
            raise AuthorizationDenied("workspace access denied")
        return capability

    def _append_status_event(
        self,
        context: WorkspaceContext,
        capability: Capability,
        capability_version_id: UUID | None,
    ) -> None:
        self._session.add(
            CapabilityStatusEvent(
                workspace_id=context.workspace_id,
                capability_id=capability.id,
                capability_version_id=capability_version_id,
                status=capability.status,
                status_epoch=capability.status_epoch,
                actor_user_id=context.actor_user_id,
            )
        )

    def _audit(
        self,
        context: WorkspaceContext,
        action: AuditAction,
        capability_id: UUID,
        correlation_id: UUID | None,
    ) -> None:
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=action,
                resource_type=ResourceType.CAPABILITY,
                resource_id=capability_id,
                correlation_id=correlation_id or uuid4(),
            )
        )

    @classmethod
    def _validate_metadata(
        cls,
        *,
        tool_identity: str,
        tool_name: str,
        display_name: str,
        description: str | None,
        metadata_digest: str,
        protocol_revision: str,
    ) -> None:
        if not tool_identity or len(tool_identity) > 256 or "\x00" in tool_identity:
            raise ValueError("invalid tool identity")
        if not tool_name or len(tool_name) > 256 or "\x00" in tool_name:
            raise ValueError("invalid tool name")
        if not display_name or len(display_name) > 256 or "\x00" in display_name:
            raise ValueError("invalid display name")
        if description is not None and (len(description) > 2048 or "\x00" in description):
            raise ValueError("invalid description")
        if cls._DIGEST.fullmatch(metadata_digest) is None:
            raise ValueError("invalid metadata digest")
        if cls._PROTOCOL_REVISION.fullmatch(protocol_revision) is None:
            raise ValueError("invalid protocol revision")
