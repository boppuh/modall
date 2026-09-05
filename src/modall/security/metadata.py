"""Storage-bound validation for untrusted metadata and JSON schemas."""

import json
import re


class MetadataValidationError(ValueError):
    """Untrusted metadata cannot be retained under the alpha policy."""


_OBVIOUS_SECRET = re.compile(
    r"(?:sk_live_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)[=:/_-][A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_SENSITIVE_JSON_FIELD = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password)", re.IGNORECASE
)


def contains_obvious_secret(value: str) -> bool:
    return _OBVIOUS_SECRET.search(value) is not None


def contains_sensitive_json(value: object) -> bool:
    """Detect credential-shaped strings and values beneath sensitive field names."""

    return _contains_obvious_secret_in_json(value)


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
    scalar_metadata = "\n".join(
        value
        for value in (tool_identity, tool_name, display_name, description, protocol_revision)
        if value is not None
    )
    if contains_obvious_secret(scalar_metadata):
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
    if contains_obvious_secret(serialized) or any(contains_sensitive_json(root) for root in roots):
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


def _contains_obvious_secret_in_json(value: object) -> bool:
    stack = [(value, False)]
    while stack:
        current, sensitive_context = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                key_is_sensitive = _SENSITIVE_JSON_FIELD.fullmatch(key) is not None
                if key_is_sensitive and isinstance(child, str) and len(child) >= 8:
                    return True
                if sensitive_context and key in {"const", "default", "enum", "example", "examples"}:
                    literals = [child]
                    while literals:
                        literal = literals.pop()
                        if isinstance(literal, str) and len(literal) >= 8:
                            return True
                        if isinstance(literal, list):
                            literals.extend(literal)
                stack.append((child, sensitive_context or key_is_sensitive))
        elif isinstance(current, list):
            stack.extend((child, sensitive_context) for child in current)
        elif isinstance(current, str) and (
            (sensitive_context and len(current) >= 8) or contains_obvious_secret(current)
        ):
            return True
    return False
