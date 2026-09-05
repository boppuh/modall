"""Shared canonical host validation for persisted and contacted endpoints."""

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from socket import inet_aton

import idna

_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class NormalizedEndpointHost:
    value: str
    parsed_ip: IPv4Address | IPv6Address | None
    is_ip_literal: bool


def normalize_endpoint_host(raw_hostname: str | None) -> NormalizedEndpointHost:
    """Normalize a URL hostname identically at persistence and transport boundaries."""

    hostname = raw_hostname.lower() if raw_hostname else ""
    if not hostname or hostname.endswith("..") or "%" in hostname:
        raise ValueError("invalid endpoint hostname")
    hostname = hostname.removesuffix(".")

    try:
        parsed_ip_literal = ip_address(hostname)
    except ValueError:
        pass
    else:
        return NormalizedEndpointHost(parsed_ip_literal.compressed, parsed_ip_literal, True)

    try:
        normalized = idna.encode(hostname).decode("ascii")
    except idna.IDNAError as exc:
        raise ValueError("invalid endpoint hostname") from exc
    if len(normalized) > 253 or any(
        _DNS_LABEL.fullmatch(label) is None for label in normalized.split(".")
    ):
        raise ValueError("invalid endpoint hostname")

    try:
        inet_aton(normalized)
    except OSError:
        is_ip_literal = False
    else:
        # Treat legacy IPv4 spellings such as 127.1 as IP literals too.
        is_ip_literal = True
    return NormalizedEndpointHost(normalized, None, is_ip_literal)
