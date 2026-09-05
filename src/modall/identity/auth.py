"""Explicit local and deployed OIDC authentication implementations."""

from collections.abc import Mapping
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

    def __init__(self, jwks_url: str) -> None:
        self._client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)

    def resolve(self, token: str) -> jwt.PyJWK:
        return self._client.get_signing_key_from_jwt(token)


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
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._resolver = resolver

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthenticationError("missing bearer token")
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
            return Principal(
                issuer=self._issuer,
                subject=subject,
                display_name=name if isinstance(name, str) else None,
            )
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("invalid bearer token") from exc


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
