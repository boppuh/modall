"""Bounded official MCP Registry search, cache, and provenance import."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import cast
from urllib.parse import unquote
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.persistence.models import (
    AuditEvent,
    RegistryEntry,
    RegistryEntryVersion,
    RegistrySearchCache,
)
from modall.registry.types import RegistrySource
from modall.security.metadata import (
    MetadataValidationError,
    contains_obvious_secret,
    contains_sensitive_json,
    contains_sensitive_url,
    validate_bounded_json,
)

OFFICIAL_REGISTRY_PROVIDER = "official"
OFFICIAL_REGISTRY_SERVERS_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"


class _SuppressRegistryRequestLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        del record
        return False


_REGISTRY_LOG_FILTER = _SuppressRegistryRequestLogs()
_REGISTRY_LOGGER_NAMES = ("httpx", "httpcore")
_REGISTRY_LOG_LOCK = Lock()
_REGISTRY_LOG_USERS = 0


@contextmanager
def _suppress_registry_request_logs() -> Iterator[None]:
    global _REGISTRY_LOG_USERS
    with _REGISTRY_LOG_LOCK:
        if _REGISTRY_LOG_USERS == 0:
            for logger_name in _REGISTRY_LOGGER_NAMES:
                logging.getLogger(logger_name).addFilter(_REGISTRY_LOG_FILTER)
        _REGISTRY_LOG_USERS += 1
    try:
        yield
    finally:
        with _REGISTRY_LOG_LOCK:
            _REGISTRY_LOG_USERS -= 1
            if _REGISTRY_LOG_USERS == 0:
                for logger_name in _REGISTRY_LOGGER_NAMES:
                    logging.getLogger(logger_name).removeFilter(_REGISTRY_LOG_FILTER)


class OfficialRegistryFailureCode(StrEnum):
    INVALID_QUERY = "invalid_query"
    UNSAFE_QUERY = "unsafe_query"
    SCANNER_FAILED = "scanner_failed"
    TIMEOUT = "timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    RESPONSE_LIMIT = "response_limit"
    INVALID_RESPONSE = "invalid_response"
    UNSAFE_METADATA = "unsafe_metadata"
    CACHE_MISS = "cache_miss"


class OfficialRegistryError(Exception):
    """A bounded, payload-free failure from the official Registry boundary."""

    def __init__(self, code: OfficialRegistryFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class OfficialRegistryLimits:
    max_query_characters: int = 256
    max_pages: int = 5
    max_items: int = 100
    max_response_bytes: int = 262_144
    max_cursor_characters: int = 1024
    total_timeout_seconds: float = 5.0
    cache_ttl: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if (
            self.max_query_characters <= 0
            or self.max_pages <= 0
            or self.max_items <= 0
            or self.max_response_bytes <= 0
            or self.max_cursor_characters <= 0
            or self.total_timeout_seconds <= 0
            or self.cache_ttl <= timedelta(0)
            or self.cache_ttl > timedelta(hours=1)
        ):
            raise ValueError("official Registry limits must be positive and cache at most one hour")


@dataclass(frozen=True, slots=True)
class OfficialRegistryItem:
    external_id: str
    source_version: str
    name: str
    description: str | None
    advertised_urls: tuple[str, ...]
    provenance_digest: str
    normalized_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class OfficialRegistrySearchResult:
    cache_id: UUID
    items: tuple[OfficialRegistryItem, ...]
    response_digest: str
    fetched_at: datetime
    expires_at: datetime
    from_cache: bool


class _DuplicateMember(ValueError):
    pass


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ValueError("non-finite number")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _decoded(value: str) -> str:
    decoded = value
    for _ in range(3):
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED) from exc
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _decoded_metadata(value: object) -> object:
    if isinstance(value, dict):
        decoded: dict[str, object] = {}
        for key, child in value.items():
            decoded_key = _decoded(key)
            if decoded_key in decoded:
                raise OfficialRegistryError(OfficialRegistryFailureCode.UNSAFE_METADATA)
            decoded[decoded_key] = _decoded_metadata(child)
        return decoded
    if isinstance(value, list):
        return [_decoded_metadata(child) for child in value]
    if isinstance(value, str):
        return _decoded(value)
    return value


def _default_metadata_scanner(value: object) -> bool:
    decoded = _decoded_metadata(value)
    if contains_sensitive_json(decoded):
        return True
    stack = [decoded]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and (
            contains_obvious_secret(current) or contains_sensitive_url(current)
        ):
            return True
    return False


class OfficialRegistryAdapter:
    """One fixed-origin, no-redirect adapter for the public Registry API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        limits: OfficialRegistryLimits | None = None,
        query_scanner: Callable[[object], bool] = _default_metadata_scanner,
        metadata_scanner: Callable[[object], bool] = _default_metadata_scanner,
    ) -> None:
        self._client = client
        self._limits = limits or OfficialRegistryLimits()
        self._query_scanner = query_scanner
        self._metadata_scanner = metadata_scanner

    @property
    def limits(self) -> OfficialRegistryLimits:
        return self._limits

    async def search(self, query: str) -> tuple[OfficialRegistryItem, ...]:
        items: list[OfficialRegistryItem] = []
        identities: set[tuple[str, str]] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        total_bytes = 0
        try:
            async with asyncio.timeout(self._limits.total_timeout_seconds):
                query = await _screen_query(query, self._limits, self._query_scanner)
                for _ in range(self._limits.max_pages):
                    params: dict[str, str | int] = {
                        "search": query,
                        "version": "latest",
                        "limit": min(100, self._limits.max_items),
                    }
                    if cursor is not None:
                        params["cursor"] = cursor
                    request = httpx.Request(
                        "GET",
                        OFFICIAL_REGISTRY_SERVERS_URL,
                        params=params,
                        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                    )
                    with _suppress_registry_request_logs():
                        response = await self._client.send(
                            request,
                            auth=None,
                            follow_redirects=False,
                            stream=True,
                        )
                    try:
                        if response.is_redirect:
                            raise OfficialRegistryError(
                                OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE
                            )
                        if response.status_code != 200:
                            raise OfficialRegistryError(
                                OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE
                            )
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                        if content_type.strip().lower() != "application/json":
                            raise OfficialRegistryError(
                                OfficialRegistryFailureCode.INVALID_RESPONSE
                            )
                        content_encoding = response.headers.get("Content-Encoding", "identity")
                        if content_encoding.strip().lower() != "identity":
                            raise OfficialRegistryError(
                                OfficialRegistryFailureCode.INVALID_RESPONSE
                            )
                        declared_length = response.headers.get("Content-Length")
                        if declared_length is not None:
                            try:
                                parsed_length = int(declared_length)
                            except ValueError as exc:
                                raise OfficialRegistryError(
                                    OfficialRegistryFailureCode.INVALID_RESPONSE
                                ) from exc
                            if (
                                parsed_length < 0
                                or total_bytes + parsed_length > self._limits.max_response_bytes
                            ):
                                raise OfficialRegistryError(
                                    OfficialRegistryFailureCode.RESPONSE_LIMIT
                                )
                        buffered = bytearray()
                        chunks = (
                            (response.content,)
                            if response.is_stream_consumed
                            else response.aiter_raw()
                        )
                        for_or_async = chunks
                        if isinstance(for_or_async, tuple):
                            buffered.extend(for_or_async[0])
                            total_bytes += len(for_or_async[0])
                            if total_bytes > self._limits.max_response_bytes:
                                raise OfficialRegistryError(
                                    OfficialRegistryFailureCode.RESPONSE_LIMIT
                                )
                        else:
                            async for chunk in for_or_async:
                                total_bytes += len(chunk)
                                if total_bytes > self._limits.max_response_bytes:
                                    raise OfficialRegistryError(
                                        OfficialRegistryFailureCode.RESPONSE_LIMIT
                                    )
                                buffered.extend(chunk)
                        body = bytes(buffered)
                    finally:
                        await response.aclose()
                    page, cursor = await self._parse_page(body)
                    if len(items) + len(page) > self._limits.max_items:
                        raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)
                    page_identities = {(item.external_id, item.source_version) for item in page}
                    if identities & page_identities:
                        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
                    identities.update(page_identities)
                    items.extend(page)
                    if cursor is None:
                        return tuple(items)
                    if cursor in seen_cursors:
                        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
                    seen_cursors.add(cursor)
        except TimeoutError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.TIMEOUT) from exc
        except httpx.DecodingError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE) from exc
        except httpx.HTTPError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE) from exc
        raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)

    async def parse_cached_items(self, values: object) -> tuple[OfficialRegistryItem, ...]:
        if not isinstance(values, list) or len(values) > self._limits.max_items:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        await self._screen(values)
        return self._normalize_items(values)

    async def _parse_page(self, body: bytes) -> tuple[tuple[OfficialRegistryItem, ...], str | None]:
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_members,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE) from exc
        try:
            validate_bounded_json(payload)
        except MetadataValidationError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT) from exc
        await self._screen(payload)
        if not isinstance(payload, dict) or set(payload) - {"servers", "metadata"}:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        servers = payload.get("servers")
        metadata = payload.get("metadata")
        if not isinstance(servers, list) or not isinstance(metadata, dict):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        count = metadata.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count != len(servers):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        cursor = metadata.get("nextCursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > self._limits.max_cursor_characters
            or any(ord(character) < 32 or ord(character) == 127 for character in cursor)
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        return self._normalize_items(servers), cursor

    async def _screen(self, value: object) -> None:
        unsafe = await _run_scanner(
            self._metadata_scanner,
            value,
            timeout_seconds=self._limits.total_timeout_seconds,
            timeout_code=OfficialRegistryFailureCode.TIMEOUT,
        )
        if unsafe:
            raise OfficialRegistryError(OfficialRegistryFailureCode.UNSAFE_METADATA)

    def _normalize_items(self, values: list[object]) -> tuple[OfficialRegistryItem, ...]:
        items: list[OfficialRegistryItem] = []
        identities: set[tuple[str, str]] = set()
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("server"), dict):
                raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
            server = cast(dict[str, object], value["server"])
            external_id = self._required_string(server.get("name"), 256)
            source_version = self._required_string(server.get("version"), 128)
            identity = (external_id, source_version)
            if identity in identities:
                raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
            identities.add(identity)
            title = server.get("title", external_id)
            name = self._required_string(title, 256)
            description_value = server.get("description")
            description = (
                None
                if description_value is None
                else self._required_string(description_value, 2048)
            )
            advertised_urls = self._advertised_urls(server)
            normalized = cast(dict[str, object], json.loads(_canonical_json(value)))
            items.append(
                OfficialRegistryItem(
                    external_id=external_id,
                    source_version=source_version,
                    name=name,
                    description=description,
                    advertised_urls=advertised_urls,
                    provenance_digest=_digest(normalized),
                    normalized_metadata=normalized,
                )
            )
        return tuple(items)

    @staticmethod
    def _required_string(value: object, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum
            or "\x00" in value
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE) from exc
        return value.strip()

    def _advertised_urls(self, server: dict[str, object]) -> tuple[str, ...]:
        urls: list[str] = []
        for key in ("websiteUrl",):
            value = server.get(key)
            if value is not None:
                urls.append(self._required_string(value, 2048))
        remotes = server.get("remotes", [])
        if not isinstance(remotes, list):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        for remote in remotes:
            if not isinstance(remote, dict):
                raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
            url = remote.get("url")
            if url is not None:
                urls.append(self._required_string(url, 2048))
        return tuple(urls)


class OfficialRegistryService:
    """Authorize search/import and keep public metadata separate from executable trust."""

    def __init__(
        self,
        session: AsyncSession,
        adapter: OfficialRegistryAdapter,
        *,
        limits: OfficialRegistryLimits | None = None,
        query_scanner: Callable[[object], bool] = _default_metadata_scanner,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._adapter = adapter
        if limits is not None and limits != adapter.limits:
            raise ValueError("service and adapter must use one official Registry limits policy")
        self._limits = adapter.limits
        self._query_scanner = query_scanner
        self._now = now

    async def search(
        self, *, context: WorkspaceContext, query: str
    ) -> OfficialRegistrySearchResult:
        await require_current_role(self._session, context, Role.ADMIN, Role.OPERATOR)
        normalized_query = await _screen_query(query, self._limits, self._query_scanner)
        query_digest = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
        now = self._utc_now()
        await _purge_expired_workspace_cache(
            self._session, workspace_id=context.workspace_id, now=now
        )
        cache = await self._session.scalar(
            select(RegistrySearchCache)
            .where(
                RegistrySearchCache.workspace_id == context.workspace_id,
                RegistrySearchCache.provider == OFFICIAL_REGISTRY_PROVIDER,
                RegistrySearchCache.query_digest == query_digest,
                RegistrySearchCache.expires_at > now,
            )
            .order_by(RegistrySearchCache.fetched_at.desc(), RegistrySearchCache.id.desc())
            .limit(1)
        )
        if cache is not None:
            try:
                items = await self._adapter.parse_cached_items(cache.normalized_results)
            except OfficialRegistryError:
                await self._session.delete(cache)
            else:
                if (
                    len(items) == cache.result_count
                    and _digest(cache.normalized_results) == cache.response_digest
                ):
                    return self._result(cache, items, from_cache=True)
                await self._session.delete(cache)

        items = await self._adapter.search(normalized_query)
        normalized_results = cast(
            list[dict[str, object]],
            json.loads(_canonical_json([item.normalized_metadata for item in items])),
        )
        fetched_at = self._utc_now()
        cache = RegistrySearchCache(
            workspace_id=context.workspace_id,
            provider=OFFICIAL_REGISTRY_PROVIDER,
            query_digest=query_digest,
            response_digest=_digest(normalized_results),
            normalized_results=normalized_results,
            result_count=len(items),
            fetched_at=fetched_at,
            expires_at=fetched_at + self._limits.cache_ttl,
        )
        self._session.add(cache)
        await self._session.flush()
        return self._result(cache, items, from_cache=False)

    async def import_cached(
        self,
        *,
        context: WorkspaceContext,
        cache_id: UUID,
        provenance_digest: str,
        correlation_id: UUID | None = None,
    ) -> RegistryEntryVersion:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        if len(provenance_digest) != 64 or any(
            character not in "0123456789abcdef" for character in provenance_digest
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.CACHE_MISS)
        now = self._utc_now()
        await _purge_expired_workspace_cache(
            self._session, workspace_id=context.workspace_id, now=now
        )
        cache = await self._session.scalar(
            select(RegistrySearchCache).where(
                RegistrySearchCache.id == cache_id,
                RegistrySearchCache.workspace_id == context.workspace_id,
                RegistrySearchCache.provider == OFFICIAL_REGISTRY_PROVIDER,
                RegistrySearchCache.expires_at > now,
            )
        )
        if cache is None or _digest(cache.normalized_results) != cache.response_digest:
            raise OfficialRegistryError(OfficialRegistryFailureCode.CACHE_MISS)
        items = await self._adapter.parse_cached_items(cache.normalized_results)
        item = next(
            (candidate for candidate in items if candidate.provenance_digest == provenance_digest),
            None,
        )
        if item is None:
            raise OfficialRegistryError(OfficialRegistryFailureCode.CACHE_MISS)

        entry = await self._session.scalar(
            select(RegistryEntry)
            .where(
                RegistryEntry.workspace_id == context.workspace_id,
                RegistryEntry.source == RegistrySource.OFFICIAL.value,
                RegistryEntry.external_id == item.external_id,
            )
            .with_for_update()
        )
        if entry is None:
            entry = RegistryEntry(
                workspace_id=context.workspace_id,
                source=RegistrySource.OFFICIAL.value,
                external_id=item.external_id,
                current_version_id=None,
            )
            self._session.add(entry)
            await self._session.flush()
        existing = await self._session.scalar(
            select(RegistryEntryVersion).where(
                RegistryEntryVersion.registry_entry_id == entry.id,
                RegistryEntryVersion.provenance_digest == item.provenance_digest,
            )
        )
        if existing is not None:
            return existing
        sequence = await self._session.scalar(
            select(func.max(RegistryEntryVersion.sequence)).where(
                RegistryEntryVersion.registry_entry_id == entry.id
            )
        )
        version = RegistryEntryVersion(
            workspace_id=context.workspace_id,
            registry_entry_id=entry.id,
            sequence=(sequence or 0) + 1,
            name=item.name,
            description=item.description,
            provenance_digest=item.provenance_digest,
            source_version=item.source_version,
            source_uri=OFFICIAL_REGISTRY_SERVERS_URL,
            normalized_metadata=item.normalized_metadata,
            imported_by_user_id=context.actor_user_id,
        )
        self._session.add(version)
        await self._session.flush()
        entry.current_version_id = version.id
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=AuditAction.REGISTRY_ENTRY_IMPORTED,
                resource_type=ResourceType.REGISTRY_ENTRY,
                resource_id=entry.id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        await self._session.flush()
        return version

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("official Registry clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _result(
        cache: RegistrySearchCache,
        items: tuple[OfficialRegistryItem, ...],
        *,
        from_cache: bool,
    ) -> OfficialRegistrySearchResult:
        return OfficialRegistrySearchResult(
            cache_id=cache.id,
            items=items,
            response_digest=cache.response_digest,
            fetched_at=cache.fetched_at,
            expires_at=cache.expires_at,
            from_cache=from_cache,
        )


async def _screen_query(
    query: str,
    limits: OfficialRegistryLimits,
    scanner: Callable[[object], bool],
) -> str:
    if (
        not query.strip()
        or len(query) > limits.max_query_characters
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
    ):
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_QUERY)
    normalized = query.strip()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_QUERY) from exc
    unsafe = await _run_scanner(
        scanner,
        normalized,
        timeout_seconds=limits.total_timeout_seconds,
        timeout_code=OfficialRegistryFailureCode.SCANNER_FAILED,
    )
    if unsafe:
        raise OfficialRegistryError(OfficialRegistryFailureCode.UNSAFE_QUERY)
    return normalized


async def _run_scanner(
    scanner: Callable[[object], bool],
    value: object,
    *,
    timeout_seconds: float,
    timeout_code: OfficialRegistryFailureCode,
) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.to_thread(scanner, value)
    except TimeoutError as exc:
        raise OfficialRegistryError(timeout_code) from exc
    except OfficialRegistryError:
        raise
    except Exception as exc:
        raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED) from exc


async def purge_expired_registry_cache(
    session: AsyncSession, *, now: datetime | None = None
) -> None:
    """Delete expired public catalog cache rows across all workspaces."""

    cutoff = now or datetime.now(UTC)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("official Registry cleanup clock must be timezone-aware")
    await session.execute(
        delete(RegistrySearchCache).where(RegistrySearchCache.expires_at <= cutoff.astimezone(UTC))
    )


async def _purge_expired_workspace_cache(
    session: AsyncSession, *, workspace_id: UUID, now: datetime
) -> None:
    await session.execute(
        delete(RegistrySearchCache).where(
            RegistrySearchCache.workspace_id == workspace_id,
            RegistrySearchCache.expires_at <= now,
        )
    )


__all__ = [
    "OFFICIAL_REGISTRY_PROVIDER",
    "OFFICIAL_REGISTRY_SERVERS_URL",
    "OfficialRegistryAdapter",
    "OfficialRegistryError",
    "OfficialRegistryFailureCode",
    "OfficialRegistryItem",
    "OfficialRegistryLimits",
    "OfficialRegistrySearchResult",
    "OfficialRegistryService",
    "purge_expired_registry_cache",
]
