"""Bounded official MCP Registry search, cache, and provenance import."""

import asyncio
import hashlib
import json
import logging
import math
import multiprocessing
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from multiprocessing.reduction import ForkingPickler
from threading import Lock
from typing import cast
from urllib.parse import unquote
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.persistence.database import register_after_rollback
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
        try:
            message = record.getMessage()
        except Exception:
            return False
        return "registry.modelcontextprotocol.io" not in message and "/v0.1/servers?" not in message


_REGISTRY_LOG_FILTER = _SuppressRegistryRequestLogs()
_REGISTRY_LOGGER_NAMES = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
)
_REGISTRY_LOG_LOCK = Lock()
_REGISTRY_LOG_USERS = 0
_SCANNER_PROCESS_CONTEXT = multiprocessing.get_context("spawn")
_SCANNER_PROCESS_LIMIT = 4
_CACHE_CLEANUP_BATCH_SIZE = 500


def _scanner_process_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = getattr(loop, "_modall_registry_scanner_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_SCANNER_PROCESS_LIMIT)
        setattr(loop, "_modall_registry_scanner_semaphore", semaphore)  # noqa: B010
    return cast(asyncio.Semaphore, semaphore)


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
    PERSISTENCE_FAILURE = "persistence_failure"


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
    max_workspace_cache_rows: int = 100
    max_workspace_cache_bytes: int = 4_194_304
    max_cursor_characters: int = 1024
    total_timeout_seconds: float = 5.0
    cache_ttl: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if (
            self.max_query_characters <= 0
            or self.max_pages <= 0
            or self.max_items <= 0
            or self.max_response_bytes <= 0
            or self.max_workspace_cache_rows <= 0
            or self.max_workspace_cache_bytes < self.max_response_bytes
            or self.max_cursor_characters <= 0
            or self.total_timeout_seconds <= 0
            or not math.isfinite(self.total_timeout_seconds)
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


class _DecodeWorkExceeded(ValueError):
    pass


class _ScannerTerminated(BaseException):
    pass


_JSON_DECODE_FAILED = object()
_CANONICAL_JSON_FAILED = object()
_CONTENT_LENGTH_FAILED = object()


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ValueError("non-finite number")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite number")
    return parsed


def _parse_content_length(value: str) -> int | object:
    try:
        return int(value)
    except ValueError:
        return _CONTENT_LENGTH_FAILED


def _is_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _decode_registry_json(body: bytes) -> object:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return _JSON_DECODE_FAILED


def _try_canonical_json(value: object) -> bytes | object:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return _CANONICAL_JSON_FAILED


def _canonical_json(value: object) -> bytes:
    encoded = _try_canonical_json(value)
    if encoded is _CANONICAL_JSON_FAILED:
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
    return cast(bytes, encoded)


def _is_bounded_json(value: object) -> bool:
    try:
        validate_bounded_json(value)
    except MetadataValidationError:
        return False
    return True


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _decoded(value: str) -> str:
    decoded = value
    for _ in range(8):
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeError:
            raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED) from None
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise _DecodeWorkExceeded("percent decoding exceeded its work budget")


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


def _scanner_process_main(scanner: Callable[[object], bool], value: object, sender: object) -> None:
    """Run an untrusted scanner where the parent can forcibly stop it."""

    connection = cast(Connection, sender)

    def terminate(signum: int, frame: object) -> None:
        del signum, frame
        raise _ScannerTerminated

    signal.signal(signal.SIGTERM, terminate)
    try:
        connection.send((True, bool(scanner(value))))
    except BaseException:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send((False, False))
    finally:
        connection.close()


class _BorrowedAsyncTransport(httpx.AsyncBaseTransport):
    """Delegate requests without closing the caller-owned transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


class OfficialRegistryAdapter:
    """One fixed-origin, no-redirect adapter for the public Registry API.

    Scanner callables cross a spawned-process boundary and must therefore be
    importable and picklable. Invalid scanner configuration fails at startup,
    before a request can be admitted.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: OfficialRegistryLimits | None = None,
        query_scanner: Callable[[object], bool] = _default_metadata_scanner,
        metadata_scanner: Callable[[object], bool] = _default_metadata_scanner,
    ) -> None:
        _require_picklable_scanner(query_scanner)
        _require_picklable_scanner(metadata_scanner)
        self._transport = transport
        self._limits = limits or OfficialRegistryLimits()
        self._query_scanner = query_scanner
        self._metadata_scanner = metadata_scanner

    @property
    def limits(self) -> OfficialRegistryLimits:
        return self._limits

    async def screen_query(self, query: str) -> str:
        """Apply the adapter's one query policy before cache access or send."""

        return await _screen_query(query, self._limits, self._query_scanner)

    async def search(self, query: str) -> tuple[OfficialRegistryItem, ...]:
        transport = None if self._transport is None else _BorrowedAsyncTransport(self._transport)
        async with httpx.AsyncClient(
            transport=transport,
            auth=None,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            return await self._search_with_client(client, query)

    async def _search_with_client(
        self, client: httpx.AsyncClient, query: str
    ) -> tuple[OfficialRegistryItem, ...]:
        items: list[OfficialRegistryItem] = []
        identities: set[tuple[str, str]] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        total_bytes = 0
        failure_code = OfficialRegistryFailureCode.RESPONSE_LIMIT
        try:
            async with asyncio.timeout(self._limits.total_timeout_seconds):
                query = await self.screen_query(query)
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
                        response = await client.send(
                            request,
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
                            parsed_length = _parse_content_length(declared_length)
                            if parsed_length is _CONTENT_LENGTH_FAILED:
                                raise OfficialRegistryError(
                                    OfficialRegistryFailureCode.INVALID_RESPONSE
                                )
                            parsed_length = cast(int, parsed_length)
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
        except TimeoutError:
            failure_code = OfficialRegistryFailureCode.TIMEOUT
        except httpx.TimeoutException:
            failure_code = OfficialRegistryFailureCode.TIMEOUT
        except httpx.DecodingError:
            failure_code = OfficialRegistryFailureCode.INVALID_RESPONSE
        except httpx.HTTPError:
            failure_code = OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE
        raise OfficialRegistryError(failure_code)

    async def parse_cached_items(self, values: object) -> tuple[OfficialRegistryItem, ...]:
        if not isinstance(values, list) or len(values) > self._limits.max_items:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        if not _is_bounded_json(values):
            raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)
        await self._screen(values)
        return self._normalize_items(values)

    async def _parse_page(self, body: bytes) -> tuple[tuple[OfficialRegistryItem, ...], str | None]:
        payload = _decode_registry_json(body)
        if payload is _JSON_DECODE_FAILED:
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
        if not _is_bounded_json(payload):
            raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)
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
        if cursor is not None and not _is_utf8(cursor):
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
            or not _is_utf8(value)
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_RESPONSE)
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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._adapter = adapter
        if limits is not None and limits != adapter.limits:
            raise ValueError("service and adapter must use one official Registry limits policy")
        self._limits = adapter.limits
        self._now = now

    async def search(
        self, *, context: WorkspaceContext, query: str
    ) -> OfficialRegistrySearchResult:
        await require_current_role(self._session, context, Role.ADMIN, Role.OPERATOR)
        try:
            async with asyncio.timeout(self._limits.total_timeout_seconds):
                return await self._search_authorized(context=context, query=query)
        except TimeoutError:
            raise OfficialRegistryError(OfficialRegistryFailureCode.TIMEOUT) from None

    async def _search_authorized(
        self, *, context: WorkspaceContext, query: str
    ) -> OfficialRegistrySearchResult:
        normalized_query = await self._adapter.screen_query(query)
        query_digest = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
        now = self._utc_now()
        cached_result, rejected_cache = await self._read_cache(
            context=context, query_digest=query_digest, now=now
        )
        if cached_result is not None:
            return cached_result

        workspace_locked = False
        if rejected_cache is not None:
            await require_current_role(
                self._session,
                context,
                Role.ADMIN,
                Role.OPERATOR,
                serialize_workspace=True,
            )
            workspace_locked = True
            cached_result, rejected_cache = await self._read_cache(
                context=context, query_digest=query_digest, now=self._utc_now()
            )
            if cached_result is not None:
                return cached_result
            if rejected_cache is not None:
                await self._purge_rejected_cache(context=context, cache=rejected_cache)

        items = await self._adapter.search(normalized_query)
        normalized_results = cast(
            list[dict[str, object]],
            json.loads(_canonical_json([item.normalized_metadata for item in items])),
        )
        normalized_bytes = _canonical_json(normalized_results)
        if len(normalized_bytes) > self._limits.max_response_bytes or not _is_bounded_json(
            normalized_results
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)
        fetched_at = self._utc_now()
        if not workspace_locked:
            await require_current_role(
                self._session,
                context,
                Role.ADMIN,
                Role.OPERATOR,
                serialize_workspace=True,
            )
        await _purge_expired_workspace_cache(
            self._session,
            workspace_id=context.workspace_id,
            now=fetched_at,
            cache_ttl=self._limits.cache_ttl,
            max_response_bytes=self._limits.max_response_bytes,
        )
        cached_result, rejected_cache = await self._read_cache(
            context=context, query_digest=query_digest, now=fetched_at
        )
        if cached_result is not None:
            return cached_result
        if rejected_cache is not None:
            await self._purge_rejected_cache(context=context, cache=rejected_cache)

        cache_usage = (
            await self._session.execute(
                select(
                    func.count(RegistrySearchCache.id),
                    func.coalesce(func.sum(RegistrySearchCache.byte_count), 0),
                ).where(RegistrySearchCache.workspace_id == context.workspace_id)
            )
        ).one()
        if (
            cache_usage[0] >= self._limits.max_workspace_cache_rows
            or cache_usage[1] + len(normalized_bytes) > self._limits.max_workspace_cache_bytes
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.RESPONSE_LIMIT)
        cache = RegistrySearchCache(
            workspace_id=context.workspace_id,
            provider=OFFICIAL_REGISTRY_PROVIDER,
            query_digest=query_digest,
            response_digest=_digest(normalized_results),
            normalized_results=normalized_results,
            result_count=len(items),
            byte_count=len(normalized_bytes),
            fetched_at=fetched_at,
            expires_at=fetched_at + self._limits.cache_ttl,
        )
        self._session.add(cache)
        if not await _flush_registry_payload(self._session):
            raise OfficialRegistryError(OfficialRegistryFailureCode.PERSISTENCE_FAILURE)
        return self._result(cache, items, from_cache=False)

    async def _read_cache(
        self, *, context: WorkspaceContext, query_digest: str, now: datetime
    ) -> tuple[OfficialRegistrySearchResult | None, RegistrySearchCache | None]:
        cache = await self._session.scalar(
            select(RegistrySearchCache)
            .where(
                RegistrySearchCache.workspace_id == context.workspace_id,
                RegistrySearchCache.provider == OFFICIAL_REGISTRY_PROVIDER,
                RegistrySearchCache.query_digest == query_digest,
                RegistrySearchCache.expires_at > now,
                RegistrySearchCache.fetched_at > now - self._limits.cache_ttl,
                RegistrySearchCache.byte_count <= self._limits.max_response_bytes,
            )
            .order_by(RegistrySearchCache.fetched_at.desc(), RegistrySearchCache.id.desc())
            .limit(1)
        )
        if cache is None:
            return None, None
        try:
            items = await self._adapter.parse_cached_items(cache.normalized_results)
            valid = (
                len(items) == cache.result_count
                and len(_canonical_json(cache.normalized_results)) == cache.byte_count
                and _digest(cache.normalized_results) == cache.response_digest
            )
        except OfficialRegistryError:
            valid = False
        if valid:
            return self._result(cache, items, from_cache=True), None
        return None, cache

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
            self._session,
            workspace_id=context.workspace_id,
            now=now,
            cache_ttl=self._limits.cache_ttl,
            max_response_bytes=self._limits.max_response_bytes,
        )
        cache = await self._session.scalar(
            select(RegistrySearchCache).where(
                RegistrySearchCache.id == cache_id,
                RegistrySearchCache.workspace_id == context.workspace_id,
                RegistrySearchCache.provider == OFFICIAL_REGISTRY_PROVIDER,
                RegistrySearchCache.expires_at > now,
                RegistrySearchCache.fetched_at > now - self._limits.cache_ttl,
                RegistrySearchCache.byte_count <= self._limits.max_response_bytes,
            )
        )
        if (
            cache is None
            or len(_canonical_json(cache.normalized_results)) != cache.byte_count
            or _digest(cache.normalized_results) != cache.response_digest
        ):
            raise OfficialRegistryError(OfficialRegistryFailureCode.CACHE_MISS)
        try:
            items = await self._adapter.parse_cached_items(cache.normalized_results)
        except OfficialRegistryError:
            await self._purge_rejected_cache(context=context, cache=cache)
            raise
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
            if not await _flush_registry_payload(self._session):
                raise OfficialRegistryError(OfficialRegistryFailureCode.PERSISTENCE_FAILURE)
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
        if not await _flush_registry_payload(self._session):
            raise OfficialRegistryError(OfficialRegistryFailureCode.PERSISTENCE_FAILURE)
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

    async def _purge_rejected_cache(
        self, *, context: WorkspaceContext, cache: RegistrySearchCache
    ) -> None:
        cache_id = cache.id
        workspace_id = context.workspace_id

        async def purge_after_rollback(cleanup_session: AsyncSession) -> None:
            await cleanup_session.execute(
                delete(RegistrySearchCache).where(
                    RegistrySearchCache.id == cache_id,
                    RegistrySearchCache.workspace_id == workspace_id,
                )
            )

        register_after_rollback(self._session, purge_after_rollback)
        await self._session.delete(cache)
        await self._session.flush()

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
    if not _is_utf8(normalized):
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_QUERY)
    unsafe = await _run_scanner(
        scanner,
        normalized,
        timeout_seconds=limits.total_timeout_seconds,
        timeout_code=OfficialRegistryFailureCode.SCANNER_FAILED,
    )
    if unsafe:
        raise OfficialRegistryError(OfficialRegistryFailureCode.UNSAFE_QUERY)
    return normalized


def _require_picklable_scanner(scanner: Callable[[object], bool]) -> None:
    try:
        ForkingPickler.dumps(scanner)
    except Exception:
        raise ValueError("official Registry scanners must be importable and picklable") from None


async def _flush_registry_payload(session: AsyncSession) -> bool:
    try:
        await session.flush()
    except SQLAlchemyError:
        return False
    return True


async def _run_scanner(
    scanner: Callable[[object], bool],
    value: object,
    *,
    timeout_seconds: float,
    timeout_code: OfficialRegistryFailureCode,
) -> bool:
    permit_acquired = False
    receiver: Connection | None = None
    sender: Connection | None = None
    process: SpawnProcess | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            semaphore = _scanner_process_semaphore()
            await semaphore.acquire()
            permit_acquired = True
            receiver, sender = _SCANNER_PROCESS_CONTEXT.Pipe(duplex=False)
            process = _SCANNER_PROCESS_CONTEXT.Process(
                target=_scanner_process_main,
                args=(scanner, value, sender),
                daemon=True,
            )
            process.start()
            sender.close()
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[None] = loop.create_future()

            def mark_ready() -> None:
                if not ready.done():
                    ready.set_result(None)

            loop.add_reader(receiver.fileno(), mark_ready)
            await ready
        loop.remove_reader(receiver.fileno())
        try:
            succeeded, result = cast(tuple[bool, bool], receiver.recv())
        except (EOFError, OSError, TypeError, ValueError):
            raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED) from None
        if not succeeded:
            raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED)
        return result
    except TimeoutError:
        raise OfficialRegistryError(timeout_code) from None
    except OfficialRegistryError:
        raise
    except Exception:
        raise OfficialRegistryError(OfficialRegistryFailureCode.SCANNER_FAILED) from None
    finally:
        if receiver is not None and "loop" in locals() and "ready" in locals():
            loop.remove_reader(receiver.fileno())
        if sender is not None:
            sender.close()
        if receiver is not None:
            receiver.close()
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.25)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        if permit_acquired:
            semaphore.release()


async def purge_expired_registry_cache(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = _CACHE_CLEANUP_BATCH_SIZE,
) -> None:
    """Delete one bounded batch of expired public catalog cache rows."""

    cutoff = now or datetime.now(UTC)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("official Registry cleanup clock must be timezone-aware")
    if batch_size <= 0 or batch_size > _CACHE_CLEANUP_BATCH_SIZE:
        raise ValueError("official Registry cleanup batch size is out of bounds")
    expired_ids = (
        select(RegistrySearchCache.id)
        .where(RegistrySearchCache.expires_at <= cutoff.astimezone(UTC))
        .order_by(RegistrySearchCache.expires_at, RegistrySearchCache.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    await session.execute(
        delete(RegistrySearchCache).where(RegistrySearchCache.id.in_(expired_ids))
    )


async def _purge_expired_workspace_cache(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    now: datetime,
    cache_ttl: timedelta | None = None,
    max_response_bytes: int | None = None,
) -> None:
    expiry_predicates = [RegistrySearchCache.expires_at <= now]
    if cache_ttl is not None:
        expiry_predicates.append(RegistrySearchCache.fetched_at <= now - cache_ttl)
    if max_response_bytes is not None:
        expiry_predicates.append(RegistrySearchCache.byte_count > max_response_bytes)
    await session.execute(
        delete(RegistrySearchCache).where(
            RegistrySearchCache.workspace_id == workspace_id,
            or_(*expiry_predicates),
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
