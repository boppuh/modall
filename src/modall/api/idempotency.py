"""Durable replay protection for non-run API mutations."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.execution.types import HmacKeyVersion
from modall.identity.repository import require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.persistence.models import ApiIdempotencyRecord


class ApiIdempotencyConflict(Exception):
    """An idempotency key was reused with a different canonical request."""


class ApiIdempotencyHistoryIncomplete(Exception):
    """A retained replay record references an unavailable HMAC key version."""


async def idempotent_mutation[ResponseT: BaseModel](
    *,
    session: AsyncSession,
    context: WorkspaceContext,
    keys: Sequence[HmacKeyVersion],
    idempotency_key: str,
    route: str,
    request_body: object,
    response_type: type[ResponseT],
    operation: Callable[[], Awaitable[ResponseT]],
    required_roles: Sequence[Role],
    record_response: Callable[[ResponseT], dict[str, object]] | None = None,
    replay_response: Callable[[dict[str, object]], Awaitable[ResponseT]] | None = None,
) -> ResponseT:
    """Serialize, replay, or persist one workspace mutation response."""

    if not 1 <= len(idempotency_key) <= 256 or any(ord(char) < 32 for char in idempotency_key):
        raise ValueError("invalid idempotency key")
    if not keys:
        raise RuntimeError("idempotency keyring is empty")
    canonical = json.dumps(
        request_body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical) > 262_144:
        raise ValueError("request exceeds idempotency limit")

    await require_current_role(session, context, *required_roles, serialize_workspace=True)
    key_bytes = idempotency_key.encode("utf-8")
    candidates = [(key.version, _hmac(key.secret, key_bytes)) for key in keys]
    now = datetime.now(UTC)
    await _require_complete_key_history(
        session, configured_versions={key.version for key in keys}, now=now
    )
    existing = await session.scalar(
        select(ApiIdempotencyRecord)
        .where(
            ApiIdempotencyRecord.workspace_id == context.workspace_id,
            ApiIdempotencyRecord.actor_user_id == context.actor_user_id,
            ApiIdempotencyRecord.method == "POST",
            ApiIdempotencyRecord.route == route,
            or_(
                *(
                    (ApiIdempotencyRecord.key_version == version)
                    & (ApiIdempotencyRecord.key_hmac == key_digest)
                    for version, key_digest in candidates
                )
            ),
        )
        .with_for_update()
    )
    if existing is not None and _utc(existing.expires_at) <= now:
        await session.delete(existing)
        await session.flush()
        existing = None
    if existing is not None:
        matching_key = next(key for key in keys if key.version == existing.key_version)
        if not hmac.compare_digest(existing.request_hmac, _hmac(matching_key.secret, canonical)):
            raise ApiIdempotencyConflict
        if replay_response is not None:
            return await replay_response(existing.response_body)
        return response_type.model_validate(existing.response_body)

    response = await operation()
    active = keys[0]
    session.add(
        ApiIdempotencyRecord(
            workspace_id=context.workspace_id,
            actor_user_id=context.actor_user_id,
            method="POST",
            route=route,
            key_version=active.version,
            key_hmac=_hmac(active.secret, key_bytes),
            request_hmac=_hmac(active.secret, canonical),
            response_body=(
                record_response(response)
                if record_response is not None
                else response.model_dump(mode="json")
            ),
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
    )
    await session.flush()
    return response


def _hmac(secret: bytes, value: bytes) -> str:
    return hmac.new(secret, value, hashlib.sha256).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _require_complete_key_history(
    session: AsyncSession, *, configured_versions: set[str], now: datetime
) -> None:
    previous: str | None = None
    for _ in range(len(configured_versions) + 1):
        statement = select(ApiIdempotencyRecord.key_version).where(
            ApiIdempotencyRecord.expires_at > now
        )
        if previous is not None:
            statement = statement.where(ApiIdempotencyRecord.key_version > previous)
        observed = await session.scalar(
            statement.order_by(ApiIdempotencyRecord.key_version).limit(1)
        )
        if observed is None:
            return
        if observed not in configured_versions:
            raise ApiIdempotencyHistoryIncomplete
        previous = observed
    raise ApiIdempotencyHistoryIncomplete


async def purge_expired_api_idempotency(
    session: AsyncSession, *, now: datetime | None = None, batch_size: int = 500
) -> int:
    """Delete one bounded batch of expired non-run replay records."""

    if not 1 <= batch_size <= 1000:
        raise ValueError("invalid idempotency cleanup batch size")
    cutoff = now or datetime.now(UTC)
    identifiers = list(
        (
            await session.scalars(
                select(ApiIdempotencyRecord.id)
                .where(ApiIdempotencyRecord.expires_at <= cutoff)
                .order_by(ApiIdempotencyRecord.expires_at, ApiIdempotencyRecord.id)
                .limit(batch_size)
            )
        ).all()
    )
    if identifiers:
        await session.execute(
            delete(ApiIdempotencyRecord).where(ApiIdempotencyRecord.id.in_(identifiers))
        )
    return len(identifiers)
