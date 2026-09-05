from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from modall.config import Settings
from modall.identity.auth import (
    AuthenticationError,
    LocalAuthenticator,
    OidcAuthenticator,
    PyJwkSigningKeyResolver,
    build_authenticator,
)
from modall.identity.types import Principal


class StaticResolver:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def resolve(self, token: str) -> bytes:
        assert token
        return self.key


def oidc_token(
    *,
    audience: str = "modall",
    issuer: str = "https://issuer.example",
    expired: bool = False,
) -> tuple[str, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    expiry = now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": "user-123",
            "name": "Ada",
            "iat": now,
            "exp": expiry,
        },
        private_key,
        algorithm="RS256",
    )
    public_key = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return token, public_key


def test_local_authenticator_is_explicit() -> None:
    authenticator = LocalAuthenticator(Settings(environment="test", local_subject="developer"))

    assert authenticator.authenticate(None) == Principal(
        issuer="modall-local", subject="developer", display_name="Local Developer"
    )
    with pytest.raises(AuthenticationError, match="not accepted"):
        authenticator.authenticate("unexpected-token")


def test_local_authenticator_rejects_deployed_mode() -> None:
    settings = Settings(
        environment="production",
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="modall",
        oidc_jwks_url="https://issuer.example/jwks",
        secret_provider="mounted_file",
    )

    with pytest.raises(ValueError, match="restricted"):
        LocalAuthenticator(settings)


def test_oidc_authenticator_validates_and_maps_principal() -> None:
    token, public_key = oidc_token(issuer="https://issuer.example/")
    authenticator = OidcAuthenticator(
        issuer="https://issuer.example/",
        audience="modall",
        resolver=StaticResolver(public_key),
    )

    assert authenticator.authenticate(token) == Principal(
        issuer="https://issuer.example/", subject="user-123", display_name="Ada"
    )


@pytest.mark.parametrize("token_kind", ["missing", "expired", "wrong-audience", "malformed"])
def test_oidc_authenticator_rejects_invalid_tokens(token_kind: str) -> None:
    token, public_key = oidc_token(
        audience="other" if token_kind == "wrong-audience" else "modall",
        expired=token_kind == "expired",
    )
    authenticator = OidcAuthenticator(
        issuer="https://issuer.example",
        audience="modall",
        resolver=StaticResolver(public_key),
    )
    submitted = (
        None if token_kind == "missing" else "not-a-jwt" if token_kind == "malformed" else token
    )

    with pytest.raises(AuthenticationError):
        authenticator.authenticate(submitted)


def test_build_authenticator_selects_local_mode() -> None:
    authenticator = build_authenticator(Settings(environment="test"))

    assert isinstance(authenticator, LocalAuthenticator)


def test_build_authenticator_selects_oidc_mode() -> None:
    settings = Settings(
        environment="test",
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="modall",
        oidc_jwks_url="https://issuer.example/jwks",
    )

    authenticator = build_authenticator(settings)

    assert isinstance(authenticator, OidcAuthenticator)


def test_jwks_resolver_returns_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = object()

    class FakeClient:
        def __init__(self, url: str, *, cache_jwk_set: bool, lifespan: int) -> None:
            assert url == "https://issuer.example/jwks"
            assert cache_jwk_set is True
            assert lifespan == 300

        def get_signing_key_from_jwt(self, token: str) -> object:
            assert token == "token"
            return expected

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver("https://issuer.example/jwks")

    assert resolver.resolve("token") is expected
