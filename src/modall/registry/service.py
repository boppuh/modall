"""Workspace-scoped connection versioning and lifecycle operations."""

import re
from typing import cast
from urllib.parse import unquote, urlsplit
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
    DiscoveryRefreshJob,
    DiscoverySnapshotCapability,
    McpToolBinding,
    SecretBinding,
    ServerConnection,
    ServerConnectionVersion,
)
from modall.registry.types import (
    CapabilityStatus,
    ConnectionLifecycle,
    RefreshJobStatus,
    Transport,
)
from modall.security.endpoints import normalize_endpoint_host
from modall.security.metadata import (
    contains_obvious_secret,
    validate_capability_scalars,
    validate_schema_payload,
)

QUALIFIED_PROTOCOL_REVISION = "2025-06-18"


class InvalidConnectionTransition(Exception):
    """A safe rejection of an invalid or stale lifecycle transition."""


class InvalidCapabilityTransition(Exception):
    """A safe rejection of an invalid or stale capability transition."""


class ConnectionService:
    _POLICY_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

    def __init__(
        self,
        session: AsyncSession,
        *,
        environment: str = "production",
        allow_loopback_http: bool = False,
    ) -> None:
        if allow_loopback_http and environment not in {"local", "test"}:
            raise ValueError("loopback HTTP is restricted to local and test environments")
        self._session = session
        self._environment = environment
        self._allow_loopback_http = allow_loopback_http

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
        self._validate_configuration(
            endpoint_url, policy_version, has_secret=secret_binding_id is not None
        )
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
        self._validate_configuration(
            endpoint_url, policy_version, has_secret=secret_binding_id is not None
        )
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
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
        await self._obsolete_refresh_jobs(context, connection.id)
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
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
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
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
        await self._obsolete_refresh_jobs(context, connection.id)
        self._audit(context, AuditAction.CONNECTION_DISABLED, connection.id, correlation_id)
        await self._session.flush()
        return connection

    async def _obsolete_refresh_jobs(self, context: WorkspaceContext, connection_id: UUID) -> None:
        outstanding_jobs = (
            await self._session.scalars(
                select(DiscoveryRefreshJob)
                .where(
                    DiscoveryRefreshJob.workspace_id == context.workspace_id,
                    DiscoveryRefreshJob.connection_id == connection_id,
                    DiscoveryRefreshJob.status.in_(
                        (RefreshJobStatus.QUEUED.value, RefreshJobStatus.LEASED.value)
                    ),
                )
                .with_for_update()
            )
        ).all()
        for job in outstanding_jobs:
            job.status = RefreshJobStatus.OBSOLETE.value
            job.lease_owner = None
            job.lease_expires_at = None

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
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
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
        if (
            connection.pending_version_id is not None
            and connection.typed_lifecycle == ConnectionLifecycle.DEGRADED
        ):
            connection.lifecycle = ConnectionLifecycle.VERIFYING.value
        connection.refresh_generation += 1
        connection.allocated_control_epoch = connection.control_epoch
        connection.allocated_target_version_id = target
        await self._session.flush()
        return connection.refresh_generation, connection.control_epoch, target

    async def complete_refresh(
        self,
        *,
        context: WorkspaceContext,
        connection_id: UUID,
        expected_version_id: UUID,
        expected_control_epoch: int,
        expected_refresh_generation: int,
    ) -> ServerConnection:
        """Consume an ordinary refresh allocation in its publication transaction."""

        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        connection = await self._locked_connection(context, connection_id)
        if (
            connection.typed_lifecycle
            not in {
                ConnectionLifecycle.VERIFYING,
                ConnectionLifecycle.ACTIVE,
                ConnectionLifecycle.DEGRADED,
            }
            or connection.pending_version_id is not None
            or connection.verified_version_id != expected_version_id
            or connection.control_epoch != expected_control_epoch
            or connection.refresh_generation != expected_refresh_generation
            or connection.allocated_control_epoch != expected_control_epoch
            or connection.allocated_target_version_id != expected_version_id
        ):
            raise InvalidConnectionTransition("connection transition rejected")
        connection.lifecycle = ConnectionLifecycle.ACTIVE.value
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
        await self._session.flush()
        return connection

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
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("connection name is not valid UTF-8") from exc
        return normalized

    def _validate_configuration(
        self, endpoint_url: str, policy_version: str, *, has_secret: bool
    ) -> None:
        if (
            not endpoint_url
            or len(endpoint_url) > 2048
            or any(ord(character) <= 32 or ord(character) == 127 for character in endpoint_url)
        ):
            raise ValueError("invalid endpoint URL")
        try:
            parsed = urlsplit(endpoint_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid endpoint URL") from exc
        try:
            normalized_host = normalize_endpoint_host(parsed.hostname)
        except ValueError as exc:
            raise ValueError("invalid endpoint URL") from exc
        hostname = normalized_host.value
        parsed_ip = normalized_host.parsed_ip
        is_ip_literal = normalized_host.is_ip_literal
        local_fixture = (
            self._environment in {"local", "test"}
            and self._allow_loopback_http
            and parsed.scheme == "http"
            and (
                hostname == "localhost"
                or hostname.endswith(".localhost")
                or (parsed_ip is not None and parsed_ip.is_loopback)
            )
        )
        if (
            (parsed.scheme != "https" and not local_fixture)
            or port == 0
            or not hostname
            or (
                not local_fixture
                and (hostname == "localhost" or hostname.endswith(".localhost") or is_ip_literal)
            )
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (not local_fixture and port not in {None, 443})
        ):
            raise ValueError("invalid endpoint URL")
        if local_fixture and has_secret:
            raise ValueError("credentials require an HTTPS endpoint")
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
            raise ValueError("invalid endpoint URL")
        try:
            decoded_path = unquote(parsed.path, errors="strict")
        except UnicodeError as exc:
            raise ValueError("invalid endpoint URL") from exc
        canonical_endpoint = (
            f"{parsed.scheme}://{hostname}{f':{port}' if port is not None else ''}{decoded_path}"
        )
        try:
            canonical_endpoint.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid endpoint URL") from exc
        if contains_obvious_secret(canonical_endpoint):
            raise ValueError("endpoint URL contains credential-shaped content")
        if self._POLICY_VERSION.fullmatch(policy_version) is None:
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
        schema_supported: bool = True,
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
            input_schema=input_schema,
            output_schema=output_schema,
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
                schema_supported=schema_supported,
            )
            if existing is not None:
                if existing.id == capability.enabled_version_id and (
                    capability.pending_version_id is not None
                ):
                    capability.pending_version_id = None
                    if capability.typed_status != CapabilityStatus.DISABLED:
                        capability.status = CapabilityStatus.ENABLED.value
                    capability.status_epoch += 1
                    self._append_status_event(context, capability, existing.id)
                elif (
                    existing.id == capability.enabled_version_id
                    and capability.typed_status == CapabilityStatus.UNAVAILABLE
                ):
                    capability.status = CapabilityStatus.ENABLED.value
                    capability.status_epoch += 1
                    self._append_status_event(context, capability, existing.id)
                elif (
                    capability.typed_status == CapabilityStatus.UNAVAILABLE
                    and capability.pending_version_id == existing.id
                ):
                    capability.status = CapabilityStatus.PENDING_REVIEW.value
                    capability.status_epoch += 1
                    self._append_status_event(context, capability, existing.id)
                elif (
                    existing.id != capability.enabled_version_id
                    and existing.id != capability.pending_version_id
                ):
                    capability.pending_version_id = existing.id
                    if capability.typed_status != CapabilityStatus.DISABLED:
                        capability.status = CapabilityStatus.PENDING_REVIEW.value
                    capability.status_epoch += 1
                    self._append_status_event(context, capability, existing.id)
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
            schema_supported=schema_supported,
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
        if capability.typed_status != CapabilityStatus.DISABLED:
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
        version = await self._session.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.id == expected_version_id,
                CapabilityVersion.capability_id == capability.id,
                CapabilityVersion.workspace_id == context.workspace_id,
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
        observed = None
        if (
            connection is not None
            and connection.current_snapshot_id is not None
            and binding is not None
        ):
            observed = await self._session.scalar(
                select(DiscoverySnapshotCapability.id).where(
                    DiscoverySnapshotCapability.workspace_id == context.workspace_id,
                    DiscoverySnapshotCapability.connection_id == capability.connection_id,
                    DiscoverySnapshotCapability.connection_version_id
                    == binding.connection_version_id,
                    DiscoverySnapshotCapability.snapshot_id == connection.current_snapshot_id,
                    DiscoverySnapshotCapability.capability_version_id == expected_version_id,
                )
            )
        if (
            binding is None
            or version is None
            or not version.schema_supported
            or connection is None
            or observed is None
            or binding.protocol_revision != QUALIFIED_PROTOCOL_REVISION
            or connection.typed_lifecycle != ConnectionLifecycle.ACTIVE
            or connection.pending_version_id is not None
            or connection.verified_version_id != binding.connection_version_id
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
        decision_version_id = capability.pending_version_id or capability.enabled_version_id
        if (
            capability.typed_status == CapabilityStatus.DISABLED
            or decision_version_id is None
            or decision_version_id != expected_version_id
        ):
            raise InvalidCapabilityTransition("capability transition rejected")
        capability.status = CapabilityStatus.DISABLED.value
        capability.status_epoch += 1
        self._append_status_event(context, capability, decision_version_id)
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
        schema_supported: bool,
    ) -> CapabilityVersion | None:
        current_ids = [
            version_id
            for version_id in (capability.enabled_version_id, capability.pending_version_id)
            if version_id is not None
        ]
        rows = await self._session.execute(
            select(CapabilityVersion, McpToolBinding)
            .join(
                McpToolBinding,
                McpToolBinding.capability_version_id == CapabilityVersion.id,
            )
            .where(
                CapabilityVersion.capability_id == capability.id,
                CapabilityVersion.workspace_id == capability.workspace_id,
                McpToolBinding.connection_version_id == connection_version_id,
                McpToolBinding.tool_name == tool_name,
                McpToolBinding.protocol_revision == protocol_revision,
            )
        )
        found_rows = rows.all()
        by_id = {version.id: (version, binding) for version, binding in found_rows}
        historical_ids = [
            version.id
            for version, _binding in sorted(
                found_rows, key=lambda row: row[0].sequence, reverse=True
            )
            if version.id not in current_ids
        ]
        for current_id in [*current_ids, *historical_ids]:
            found = by_id.get(current_id)
            if found is None:
                continue
            version, _binding = found
            if (
                version.display_name == display_name
                and version.description == description
                and version.input_schema == input_schema
                and version.output_schema == output_schema
                and version.metadata_digest == metadata_digest
                and version.schema_supported == schema_supported
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
        input_schema: dict[str, object],
        output_schema: dict[str, object] | None,
        metadata_digest: str,
        protocol_revision: str,
    ) -> None:
        if cls._DIGEST.fullmatch(metadata_digest) is None:
            raise ValueError("invalid metadata digest")
        if cls._PROTOCOL_REVISION.fullmatch(protocol_revision) is None:
            raise ValueError("invalid protocol revision")
        validate_capability_scalars(
            tool_identity=tool_identity,
            tool_name=tool_name,
            display_name=display_name,
            description=description,
            protocol_revision=protocol_revision,
        )
        validate_schema_payload(input_schema, output_schema)
