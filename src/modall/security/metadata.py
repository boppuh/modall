"""Storage-bound validation for untrusted metadata and JSON schemas."""

import json
import math
import re
from collections import Counter
from urllib.parse import unquote, urlsplit

import re2  # type: ignore[import-untyped]

from modall.security.endpoints import normalize_endpoint_host


class MetadataValidationError(ValueError):
    """Untrusted metadata cannot be retained under the alpha policy."""


_OBVIOUS_SECRET = re.compile(
    r"(?:sk_live_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_GENERIC_SECRET_VALUE = re.compile(
    r"(?:api[_-]?key|(?:(?:access|refresh|session|auth|bearer)[_-]?)?token|credential|"
    r"private[_-]?key|secret|password)"
    r"[\"']?(?:[=:/][\s\x00-\x1f\x7f-\x9f]*|\s+)"
    r"[\"'`]?\s*(?P<value>[A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_GENERIC_SECRET_MARKER = re.compile(
    r"(?:api[_-]?key|(?:(?:access|refresh|session|auth|bearer)[_-]?)?token|credential|"
    r"private[_-]?key|secret|password)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?:authorization|authentication)\s*[:=]\s*(?:bearer\s+)?"
    r"(?P<assigned>[A-Za-z0-9._~+/=\-]{8,})|"
    r"bearer\s+(?P<bearer>[A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_SENSITIVE_JSON_FIELD = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|(?:access|refresh|session|auth|bearer)?[_-]?token|"
    r"authorization|authentication|credentials?|private[_-]?key|secret|password)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_OPAQUE_ANNOTATION_VALUE = re.compile(r"[A-Za-z0-9._~+/=\-]{8,}\Z")
_SENSITIVE_MARKER_PREFIX = re.compile(
    r"(?:api[-_]?key|(?:(?:access|refresh|session|auth|bearer)[-_]?)?token|credential|"
    r"private[-_]?key|secret|password)"
    r"[-_](?P<value>[A-Za-z0-9._~+/=\-]{8,})\Z",
    re.IGNORECASE,
)
_SENSITIVE_PATH_MARKER = re.compile(
    r"(?:^|[!$&'()*+,.;:=@/])(?:api[-_]?key|"
    r"(?:(?:access|refresh|session|auth|bearer)[-_]?)?token|credential|"
    r"private[-_]?key|secret|password)"
    r"[-_](?P<value>[A-Za-z0-9._~+/=\-]{8,}?)(?=$|[!$&'()*,;:=@])",
    re.IGNORECASE,
)
_SENSITIVE_HOST_LABEL_MARKER = re.compile(
    r"(?:^|.*-)(?:api[-_]?key|"
    r"(?:(?:access|refresh|session|auth|bearer)[-_]?)?token|credential|"
    r"private[-_]?key|secret|password)"
    r"[-_](?P<value>[A-Za-z0-9_~+=\-]{8,})\Z",
    re.IGNORECASE,
)
_URL_CANDIDATE = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_AUTH_MODE_WORDS = {
    "anonymous",
    "api",
    "apikey",
    "auth",
    "authentication",
    "authorization",
    "basic",
    "bearer",
    "client",
    "code",
    "credentials",
    "device",
    "digest",
    "disabled",
    "enabled",
    "key",
    "jwt",
    "mtls",
    "none",
    "oauth",
    "oauth2",
    "oidc",
    "optional",
    "pkce",
    "public",
    "required",
    "supported",
    "unsupported",
}
_SENSITIVE_FIELD_PROBES = (
    "api_key",
    "apiKey",
    "access_token",
    "refresh_token",
    "session_token",
    "auth_token",
    "bearer_token",
    "credential",
    "credentials",
    "authorization",
    "authentication",
    "private_key",
    "privateKey",
    "client_secret",
    "clientSecret",
    "secret",
    "password",
    "token",
)


def _is_sensitive_field(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized)
    return _SENSITIVE_JSON_FIELD.search(normalized) is not None


def _pattern_matches_sensitive_field(pattern: str) -> bool:
    try:
        compiled = re2.compile(pattern)
    except re2.error:
        return True
    return any(compiled.search(probe) is not None for probe in _SENSITIVE_FIELD_PROBES)


def contains_obvious_secret(value: str) -> bool:
    if _OBVIOUS_SECRET.search(value) is not None:
        return True
    marker_starts = [match.start() for match in _GENERIC_SECRET_MARKER.finditer(value)]
    for index, start in enumerate(marker_starts):
        end = marker_starts[index + 1] if index + 1 < len(marker_starts) else len(value)
        match = _GENERIC_SECRET_VALUE.match(value, start, end)
        if match is not None and _looks_like_secret_candidate(match.group("value")):
            return True
    for match in _AUTHORIZATION_VALUE.finditer(value):
        candidate = match.group("assigned") or match.group("bearer")
        if match.group("assigned") is not None and _is_auth_mode(candidate):
            continue
        if _looks_like_secret_candidate(candidate):
            return True
    return False


def _entropy_bits(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return sum(count * math.log2(length / count) for count in counts.values())


def _looks_like_opaque_value(value: str) -> bool:
    if len(value) < 12 or _OPAQUE_ANNOTATION_VALUE.fullmatch(value) is None:
        return False
    length = len(value)
    return _entropy_bits(value) >= length * 3.5


def _looks_like_secret_candidate(value: str) -> bool:
    return _looks_like_opaque_value(value) or (
        len(value) >= 16 and value.isdecimal() and _entropy_bits(value) >= 64
    )


def _is_auth_mode(value: str) -> bool:
    words = [word for word in re.split(r"[_-]+", value.lower()) if word]
    return bool(words) and all(word in _AUTH_MODE_WORDS for word in words)


def _looks_like_marker_suffix(value: str) -> bool:
    return _looks_like_secret_candidate(value) or _looks_like_secret_candidate(
        value.replace(".", "").replace("/", "")
    )


def _looks_like_internal_marker_suffix(value: str) -> bool:
    words = value.split("-")
    if len(words) > 1 and all(word.isalpha() and word.islower() for word in words):
        return False
    return _looks_like_marker_suffix(value)


def contains_sensitive_hostname(hostname: str) -> bool:
    """Detect credential markers adjacent to opaque DNS-label content."""

    labels = hostname.rstrip(".").split(".")
    adjacent_value = any(
        _is_sensitive_field(label) and _looks_like_marker_suffix(labels[index + 1])
        for index, label in enumerate(labels[:-1])
    )
    cross_label_value = any(
        _is_sensitive_field(label)
        and any(
            _looks_like_marker_suffix(".".join(labels[index + 1 : end]))
            for end in range(index + 2, len(labels) + 1)
        )
        for index, label in enumerate(labels[:-1])
    )
    prefixed_value = any(
        (match := _SENSITIVE_MARKER_PREFIX.fullmatch(label)) is not None
        and _looks_like_marker_suffix(match.group("value"))
        for label in labels
    )
    internal_value = any(
        (match := _SENSITIVE_HOST_LABEL_MARKER.fullmatch(label)) is not None
        and _looks_like_marker_suffix(match.group("value"))
        for label in labels
    )
    return adjacent_value or cross_label_value or prefixed_value or internal_value


def contains_sensitive_url_path(path: str) -> bool:
    """Detect marker-prefixed opaque values, including decoded separators."""

    segment_match = any(
        (match := _SENSITIVE_MARKER_PREFIX.fullmatch(segment)) is not None
        and _looks_like_marker_suffix(match.group("value"))
        for segment in path.split("/")
    )
    internal_segment_match = any(
        (match := _SENSITIVE_HOST_LABEL_MARKER.fullmatch(segment)) is not None
        and _looks_like_internal_marker_suffix(match.group("value"))
        for segment in path.split("/")
    )
    return (
        segment_match
        or internal_segment_match
        or any(
            _looks_like_marker_suffix(match.group("value"))
            for match in _SENSITIVE_PATH_MARKER.finditer(path)
        )
    )


def contains_sensitive_url(value: str) -> bool:
    """Detect credential-shaped host or path content in embedded HTTP URLs."""

    candidates = [value]
    decoded = value
    for _ in range(3):
        try:
            next_decoded = unquote(decoded, errors="strict")
        except UnicodeError:
            break
        if next_decoded == decoded:
            break
        candidates.append(next_decoded)
        decoded = next_decoded
    for match in (
        match for candidate in candidates for match in _URL_CANDIDATE.finditer(candidate)
    ):
        candidate = match.group().rstrip(".,;:!?)]")
        try:
            decoded_candidate = decode_safe_url_path(candidate)
        except ValueError:
            decoded_candidate = candidate
        if contains_sensitive_url_path(decoded_candidate) or contains_obvious_secret(
            decoded_candidate
        ):
            return True
        try:
            parsed = urlsplit(candidate)
            host = normalize_endpoint_host(parsed.hostname).value
            decoded_path = decode_safe_url_path(parsed.path)
            decoded_username = (
                decode_safe_url_path(parsed.username) if parsed.username is not None else None
            )
            decoded_query = decode_safe_url_path(parsed.query)
            decoded_fragment = decode_safe_url_path(parsed.fragment)
        except ValueError:
            continue
        if (
            parsed.password is not None
            or contains_sensitive_hostname(host)
            or contains_sensitive_url_path(decoded_path)
            or contains_obvious_secret(decoded_path)
            or contains_sensitive_url_path(decoded_query)
            or contains_sensitive_url_path(decoded_fragment)
            or contains_obvious_secret(decoded_query)
            or contains_obvious_secret(decoded_fragment)
            or (
                decoded_username is not None
                and (
                    contains_obvious_secret(decoded_username)
                    or contains_sensitive_url_path(decoded_username)
                )
            )
        ):
            return True
    return False


def decode_safe_url_path(path: str) -> str:
    """Strictly decode a URL path while rejecting ambiguous or control content."""

    if re.search(r"%(?![0-9A-Fa-f]{2})", path):
        raise ValueError("invalid endpoint URL")
    try:
        decoded_path = unquote(path, errors="strict")
    except UnicodeError as exc:
        raise ValueError("invalid endpoint URL") from exc
    if re.search(r"%[0-9A-Fa-f]{2}", decoded_path) or any(
        character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
        for character in decoded_path
    ):
        raise ValueError("invalid endpoint URL")
    return decoded_path


def contains_sensitive_json(value: object) -> bool:
    """Detect credential-shaped strings and values beneath sensitive field names."""

    return _contains_obvious_secret_in_json(value)


def contains_sensitive_schema(value: object) -> bool:
    """Screen schema metadata without treating property names as stored values."""

    stack = [(value, False)]
    visited: set[tuple[int, bool]] = set()
    anchors = _index_schema_anchors(value)
    if anchors is None:
        return True
    reference_cache: dict[str, object | None] = {}
    literal_keys = {"const", "default", "enum", "example", "examples"}
    annotation_keys = {"$comment", "description", "title"}
    schema_map_keys = {"$defs", "definitions", "dependentSchemas", "patternProperties"}
    schema_keys = {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
    schema_array_keys = {"allOf", "anyOf", "oneOf", "prefixItems"}
    while stack:
        current, sensitive_property = stack.pop()
        visit = (id(current), sensitive_property)
        if visit in visited:
            continue
        visited.add(visit)
        if isinstance(current, dict):
            for key, child in current.items():
                if (
                    sensitive_property
                    and key in {"$ref", "$dynamicRef", "$recursiveRef"}
                    and isinstance(child, str)
                    and child.startswith("#")
                ):
                    if child not in reference_cache:
                        reference_cache[child] = _resolve_local_schema_reference(
                            value, child, anchors
                        )
                    target = reference_cache[child]
                    if target is not None:
                        stack.append((target, True))
                    continue
                if key == "properties" and isinstance(child, dict):
                    for property_name, property_schema in child.items():
                        stack.append(
                            (
                                property_schema,
                                sensitive_property or _is_sensitive_field(property_name),
                            )
                        )
                    continue
                if key in {"patternProperties", "dependentSchemas"} and isinstance(child, dict):
                    stack.extend(
                        (
                            schema,
                            sensitive_property
                            or (
                                _pattern_matches_sensitive_field(property_pattern)
                                if key == "patternProperties"
                                else _is_sensitive_field(property_pattern)
                            ),
                        )
                        for property_pattern, schema in child.items()
                    )
                    continue
                if key in schema_map_keys and isinstance(child, dict):
                    stack.extend((schema, sensitive_property) for schema in child.values())
                    continue
                if key in schema_keys and isinstance(child, (dict, bool)):
                    stack.append((child, sensitive_property))
                    continue
                if key in schema_array_keys and isinstance(child, list):
                    stack.extend((schema, sensitive_property) for schema in child)
                    continue
                if (
                    sensitive_property
                    and key in literal_keys
                    and _contains_obvious_secret_in_json(child, initially_sensitive=True)
                ):
                    return True
                if (
                    sensitive_property
                    and key in annotation_keys
                    and isinstance(child, str)
                    and (_looks_like_secret_candidate(child) or contains_obvious_secret(child))
                ):
                    return True
                if key not in literal_keys and contains_sensitive_json({key: child}):
                    return True
                if key in literal_keys and contains_sensitive_json(child):
                    return True
        elif isinstance(current, str):
            if (
                sensitive_property and _looks_like_secret_candidate(current)
            ) or contains_obvious_secret(current):
                return True
        elif (
            sensitive_property
            and isinstance(current, (int, float))
            and not isinstance(current, bool)
            and len(str(current)) >= 8
        ):
            return True
    return False


def _index_schema_anchors(root: object) -> dict[str, object] | None:
    anchors: dict[str, object] = {}
    candidates = [root]
    visited: set[int] = set()
    schema_map_keys = {"$defs", "definitions", "dependentSchemas", "patternProperties"}
    schema_keys = {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
    schema_array_keys = {"allOf", "anyOf", "oneOf", "prefixItems"}
    while candidates:
        current = candidates.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(current, dict):
            for keyword in ("$anchor", "$dynamicAnchor"):
                anchor = current.get(keyword)
                if isinstance(anchor, str):
                    if anchor in anchors:
                        return None
                    anchors[anchor] = current
            properties = current.get("properties")
            if isinstance(properties, dict):
                candidates.extend(properties.values())
            for key in schema_map_keys:
                schemas = current.get(key)
                if isinstance(schemas, dict):
                    candidates.extend(schemas.values())
            for key in schema_keys:
                schema = current.get(key)
                if isinstance(schema, (dict, bool)):
                    candidates.append(schema)
            for key in schema_array_keys:
                schemas = current.get(key)
                if isinstance(schemas, list):
                    candidates.extend(schemas)
    return anchors


def _resolve_local_schema_reference(
    root: object, reference: str, anchors: dict[str, object]
) -> object | None:
    fragment = unquote(reference[1:])
    if not fragment:
        return root
    if fragment.startswith("/"):
        current = root
        for encoded_part in fragment[1:].split("/"):
            part = encoded_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None
        return current
    return anchors.get(fragment)


def validate_capability_scalars(
    *,
    tool_identity: str,
    tool_name: str,
    display_name: str,
    description: str | None,
    protocol_revision: str,
) -> None:
    if not tool_identity or len(tool_identity) > 256 or "\x00" in tool_identity:
        raise MetadataValidationError("invalid tool identity")
    if not tool_name or len(tool_name) > 256 or "\x00" in tool_name:
        raise MetadataValidationError("invalid tool name")
    if not display_name or len(display_name) > 256 or "\x00" in display_name:
        raise MetadataValidationError("invalid display name")
    if description is not None and (len(description) > 2048 or "\x00" in description):
        raise MetadataValidationError("invalid description")
    try:
        for value in (tool_identity, tool_name, display_name, description, protocol_revision):
            if value is not None:
                value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MetadataValidationError("capability metadata is not valid UTF-8") from exc
    if any(
        contains_obvious_secret(value)
        for value in (tool_identity, tool_name, display_name, description, protocol_revision)
        if value is not None
    ):
        raise MetadataValidationError("capability metadata contains credential-shaped content")


def validate_schema_payload(
    input_schema: dict[str, object], output_schema: dict[str, object] | None
) -> None:
    """Require schemas to fit the exact structural and storage policy bounds."""

    roots: tuple[object, ...] = (
        (input_schema,) if output_schema is None else (input_schema, output_schema)
    )
    stack = [(root, 1) for root in roots]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if depth > 32 or node_count > 4096:
            raise MetadataValidationError("schema exceeds structural limits")
        if isinstance(value, dict):
            if len(value) > 1024:
                raise MetadataValidationError("schema exceeds property limits")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 8192:
                    raise MetadataValidationError("schema contains an invalid key")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > 1024:
                raise MetadataValidationError("schema exceeds item limits")
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > 8192:
                raise MetadataValidationError("schema string exceeds limits")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise MetadataValidationError("schema contains a non-JSON value")
    try:
        serialized = json.dumps(roots, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise MetadataValidationError("schema is not bounded JSON") from exc
    if len(encoded) > 131_072:
        raise MetadataValidationError("schema exceeds serialized size limit")
    if contains_obvious_secret(serialized) or any(
        contains_sensitive_schema(root) for root in roots
    ):
        raise MetadataValidationError("schema contains credential-shaped content")


def validate_bounded_json(value: object) -> None:
    """Apply persistence structural bounds to a complete normalized JSON value."""

    stack = [(value, 1)]
    node_count = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if depth > 32 or node_count > 4096:
            raise MetadataValidationError("metadata exceeds structural limits")
        if isinstance(current, dict):
            if len(current) > 1024:
                raise MetadataValidationError("metadata exceeds property limits")
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > 8192:
                    raise MetadataValidationError("metadata contains an invalid key")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > 1024:
                raise MetadataValidationError("metadata exceeds item limits")
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current) > 8192:
                raise MetadataValidationError("metadata string exceeds limits")
        elif current is not None and not isinstance(current, (bool, int, float)):
            raise MetadataValidationError("metadata contains a non-JSON value")


def _contains_obvious_secret_in_json(value: object, *, initially_sensitive: bool = False) -> bool:
    stack = [(value, initially_sensitive)]
    while stack:
        current, sensitive_context = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if sensitive_context and _looks_like_secret_candidate(key):
                    return True
                key_is_sensitive = _is_sensitive_field(key)
                child_is_auth_mode = (
                    isinstance(child, str)
                    and _is_auth_mode(child)
                    and (sensitive_context or (key_is_sensitive and "auth" in key.lower()))
                )
                child_is_expiration_metadata = (
                    (key_is_sensitive or sensitive_context)
                    and isinstance(child, (int, float))
                    and not isinstance(child, bool)
                    and _is_expiration_field(key)
                )
                if (
                    key_is_sensitive
                    and isinstance(child, str)
                    and not child_is_auth_mode
                    and _looks_like_secret_candidate(child)
                ):
                    return True
                if sensitive_context and key in {"const", "default", "enum", "example", "examples"}:
                    literals = [child]
                    while literals:
                        literal = literals.pop()
                        if isinstance(literal, str) and _looks_like_secret_candidate(literal):
                            return True
                        if isinstance(literal, list):
                            literals.extend(literal)
                next_sensitive_context = (
                    False
                    if child_is_auth_mode or child_is_expiration_metadata
                    else sensitive_context or key_is_sensitive
                )
                stack.append((child, next_sensitive_context))
        elif isinstance(current, list):
            stack.extend((child, sensitive_context) for child in current)
        elif (
            sensitive_context
            and isinstance(current, (int, float))
            and not isinstance(current, bool)
            and len(str(current)) >= 8
        ) or (
            isinstance(current, str)
            and (
                (sensitive_context and _looks_like_secret_candidate(current))
                or contains_obvious_secret(current)
            )
        ):
            return True
    return False


def _is_expiration_field(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    words = {word for word in re.split(r"[^a-z0-9]+", normalized) if word}
    return bool(words & {"expiration", "expires", "expiry", "lifetime", "ttl"})
