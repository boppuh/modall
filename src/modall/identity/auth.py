"""Explicit local and deployed OIDC authentication implementations."""

import math
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Protocol, cast

import jwt
from jwt import PyJWKClient

from modall.config import Settings
from modall.identity.types import Principal


class AuthenticationError(Exception):
    """Raised when caller authentication fails without exposing token details."""


class Authenticator(Protocol):
    def authenticate(self, token: str | None) -> Principal: ...


class SigningKeyResolver(Protocol):
    def resolve(self, token: str) -> jwt.PyJWK | str | bytes: ...


class PyJwkSigningKeyResolver:
    """Resolve signing keys from the configured TLS JWKS endpoint."""

    def __init__(
        self,
        jwks_url: str,
        *,
        refresh_interval_seconds: float = 30,
        negative_ttl_seconds: float = 60,
        max_negative_kids: int = 256,
        timeout_seconds: float = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        timing_bounds = (refresh_interval_seconds, negative_ttl_seconds, timeout_seconds)
        if any(value <= 0 or not math.isfinite(value) for value in timing_bounds):
            raise ValueError("JWKS timing bounds must be positive")
        if max_negative_kids <= 0:
            raise ValueError("JWKS negative-cache bound must be positive")
        self._client = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=300,
            timeout=timeout_seconds,
        )
        self._refresh_interval_seconds = refresh_interval_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._max_negative_kids = max_negative_kids
        self._clock = clock
        self._lock = Lock()
        self._signing_keys: list[jwt.PyJWK] | None = None
        self._next_refresh_at = 0.0
        self._negative_kids: OrderedDict[str, float] = OrderedDict()

    def resolve(self, token: str) -> jwt.PyJWK:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if kid is not None and (not isinstance(kid, str) or not kid or len(kid) > 256):
            raise AuthenticationError("invalid bearer token")

        with self._lock:
            now = self._clock()
            if self._signing_keys is None:
                if now < self._next_refresh_at:
                    raise AuthenticationError("signing keys temporarily unavailable")
                self._next_refresh_at = now + self._refresh_interval_seconds
                self._signing_keys = self._client.get_signing_keys()
            elif now >= self._next_refresh_at:
                self._next_refresh_at = now + self._refresh_interval_seconds
                # Fail closed if refresh fails instead of continuing with a key set
                # the issuer may have revoked.
                self._signing_keys = None
                self._signing_keys = self._client.get_signing_keys(refresh=True)

            if kid is None:
                if len(self._signing_keys) == 1:
                    return self._signing_keys[0]
                raise AuthenticationError("ambiguous signing key")

            signing_key = self._client.match_kid(self._signing_keys, kid)
            if signing_key is not None:
                self._negative_kids.pop(kid, None)
                return signing_key

            negative_expiry = self._negative_kids.get(kid)
            if negative_expiry is not None and negative_expiry > now:
                raise AuthenticationError("invalid bearer token")
            self._negative_kids.pop(kid, None)

            self._negative_kids[kid] = now + self._negative_ttl_seconds
            self._negative_kids.move_to_end(kid)
            while len(self._negative_kids) > self._max_negative_kids:
                self._negative_kids.popitem(last=False)
            raise AuthenticationError("invalid bearer token")


class LocalAuthenticator:
    """Return one explicit development principal without accepting bearer data."""

    def __init__(self, settings: Settings) -> None:
        if settings.auth_mode != "local" or settings.environment not in {"local", "test"}:
            raise ValueError("local authentication is restricted to local and test environments")
        self._subject = settings.local_subject

    def authenticate(self, token: str | None) -> Principal:
        if token is not None:
            raise AuthenticationError("bearer tokens are not accepted in local mode")
        return Principal(
            issuer="modall-local", subject=self._subject, display_name="Local Developer"
        )


class OidcAuthenticator:
    """Validate provider tokens against pinned issuer, audience, and algorithms."""

    _ALGORITHMS = ("RS256", "ES256")

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        resolver: SigningKeyResolver,
    ) -> None:
        # OIDC issuer identifiers are security identifiers, not URLs to normalize.
        self._issuer = issuer
        self._audience = audience
        self._resolver = resolver

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthenticationError("missing bearer token")
        if len(token) > 16_384:
            raise AuthenticationError("bearer token exceeds size limit")
        try:
            key = self._resolver.resolve(token)
            claims = cast(
                Mapping[str, object],
                jwt.decode(
                    token,
                    key=key,
                    algorithms=list(self._ALGORITHMS),
                    audience=self._audience,
                    issuer=self._issuer,
                    leeway=30,
                    options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                ),
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject.strip():
                raise AuthenticationError("invalid subject claim")
            name = claims.get("name")
            audiences = claims["aud"]
            authorized_party = claims.get("azp")
            has_multiple_audiences = isinstance(audiences, list) and len(audiences) > 1
            if (has_multiple_audiences and authorized_party is None) or (
                authorized_party is not None and authorized_party != self._audience
            ):
                raise AuthenticationError("invalid authorized party")
            return Principal(
                issuer=self._issuer,
                subject=subject,
                display_name=name if isinstance(name, str) else None,
            )
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise AuthenticationError("invalid bearer token") from None


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == "local":
        return LocalAuthenticator(settings)
    if (
        settings.oidc_issuer is None
        or settings.oidc_audience is None
        or settings.oidc_jwks_url is None
    ):
        raise ValueError("complete OIDC settings are required")
    return OidcAuthenticator(
        issuer=str(settings.oidc_issuer),
        audience=settings.oidc_audience,
        resolver=PyJwkSigningKeyResolver(str(settings.oidc_jwks_url)),
    )
