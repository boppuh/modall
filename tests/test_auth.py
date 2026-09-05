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
    audience: str | list[str] = "modall",
    issuer: str = "https://issuer.example",
    authorized_party: str | None = None,
    expired: bool = False,
) -> tuple[str, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    expiry = now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "name": "Ada",
        "iat": now,
        "exp": expiry,
    }
    if authorized_party is not None:
        claims["azp"] = authorized_party
    token = jwt.encode(
        claims,
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


def test_oidc_authenticator_validates_multi_audience_authorized_party() -> None:
    token, public_key = oidc_token(audience=["modall", "other"], authorized_party="modall")
    authenticator = OidcAuthenticator(
        issuer="https://issuer.example",
        audience="modall",
        resolver=StaticResolver(public_key),
    )

    assert authenticator.authenticate(token).subject == "user-123"


@pytest.mark.parametrize("authorized_party", [None, "other"])
def test_oidc_authenticator_rejects_invalid_multi_audience_authorized_party(
    authorized_party: str | None,
) -> None:
    token, public_key = oidc_token(audience=["modall", "other"], authorized_party=authorized_party)
    authenticator = OidcAuthenticator(
        issuer="https://issuer.example",
        audience="modall",
        resolver=StaticResolver(public_key),
    )

    with pytest.raises(AuthenticationError):
        authenticator.authenticate(token)


def test_oidc_authenticator_suppresses_token_derived_causes() -> None:
    class FailingResolver:
        def resolve(self, token: str) -> bytes:
            raise jwt.PyJWKClientError(f'attacker-controlled token data: "{token}"')

    authenticator = OidcAuthenticator(
        issuer="https://issuer.example",
        audience="modall",
        resolver=FailingResolver(),
    )

    with pytest.raises(AuthenticationError) as caught:
        authenticator.authenticate("attacker-kid")
    assert caught.value.__cause__ is None


def test_oidc_authenticator_rejects_oversize_tokens_before_resolution() -> None:
    class UnexpectedResolver:
        def resolve(self, token: str) -> bytes:
            raise AssertionError("oversize token reached key resolution")

    authenticator = OidcAuthenticator(
        issuer="https://issuer.example",
        audience="modall",
        resolver=UnexpectedResolver(),
    )

    with pytest.raises(AuthenticationError, match="size limit"):
        authenticator.authenticate("x" * 16_385)


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
        def __init__(
            self,
            url: str,
            *,
            cache_jwk_set: bool,
            lifespan: int,
            timeout: float,
        ) -> None:
            assert url == "https://issuer.example/jwks"
            assert cache_jwk_set is True
            assert lifespan == 300
            assert timeout == 2

        def get_signing_keys(self, *, refresh: bool = False) -> list[object]:
            assert refresh is False
            return [expected]

        def match_kid(self, signing_keys: list[object], kid: str) -> object | None:
            assert signing_keys == [expected]
            return expected if kid == "known" else None

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver("https://issuer.example/jwks")
    token = jwt.encode({}, "s" * 32, algorithm="HS256", headers={"kid": "known"})

    assert resolver.resolve(token) is expected


@pytest.mark.parametrize("key_count", [1, 2])
def test_jwks_resolver_accepts_missing_kid_only_for_one_key(
    monkeypatch: pytest.MonkeyPatch, key_count: int
) -> None:
    keys = [object() for _ in range(key_count)]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_signing_keys(self, *, refresh: bool = False) -> list[object]:
            return keys

        def match_kid(self, signing_keys: list[object], kid: str) -> None:
            raise AssertionError("missing kid should not use kid matching")

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver("https://issuer.example/jwks")
    token = jwt.encode({}, "s" * 32, algorithm="HS256")

    if key_count == 1:
        assert resolver.resolve(token) is keys[0]
    else:
        with pytest.raises(AuthenticationError, match="ambiguous"):
            resolver.resolve(token)


def test_jwks_resolver_bounds_unknown_key_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_calls: list[bool] = []
    now = [0.0]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_signing_keys(self, *, refresh: bool = False) -> list[object]:
            refresh_calls.append(refresh)
            return []

        def match_kid(self, signing_keys: list[object], kid: str) -> None:
            return None

    def token(kid: str) -> str:
        return jwt.encode({}, "s" * 32, algorithm="HS256", headers={"kid": kid})

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver(
        "https://issuer.example/jwks",
        refresh_interval_seconds=30,
        clock=lambda: now[0],
    )

    with pytest.raises(AuthenticationError):
        resolver.resolve(token("unknown-a"))
    with pytest.raises(AuthenticationError):
        resolver.resolve(token("unknown-b"))
    assert True not in refresh_calls

    now[0] = 31
    with pytest.raises(AuthenticationError):
        resolver.resolve(token("unknown-c"))
    with pytest.raises(AuthenticationError):
        resolver.resolve(token("unknown-c"))
    assert refresh_calls.count(True) == 1


def test_jwks_resolver_periodically_replaces_revoked_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = object()
    now = [0.0]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_signing_keys(self, *, refresh: bool = False) -> list[object]:
            return [] if refresh else [old_key]

        def match_kid(self, signing_keys: list[object], kid: str) -> object | None:
            return old_key if old_key in signing_keys and kid == "old" else None

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver(
        "https://issuer.example/jwks",
        refresh_interval_seconds=30,
        clock=lambda: now[0],
    )
    token = jwt.encode({}, "s" * 32, algorithm="HS256", headers={"kid": "old"})

    assert resolver.resolve(token) is old_key
    now[0] = 31
    with pytest.raises(AuthenticationError):
        resolver.resolve(token)


def test_jwks_scheduled_refresh_overrides_negative_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_key = object()
    now = [0.0]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_signing_keys(self, *, refresh: bool = False) -> list[object]:
            return [new_key] if refresh else []

        def match_kid(self, signing_keys: list[object], kid: str) -> object | None:
            return new_key if new_key in signing_keys and kid == "new" else None

    monkeypatch.setattr("modall.identity.auth.PyJWKClient", FakeClient)
    resolver = PyJwkSigningKeyResolver(
        "https://issuer.example/jwks",
        refresh_interval_seconds=30,
        negative_ttl_seconds=60,
        clock=lambda: now[0],
    )
    token = jwt.encode({}, "s" * 32, algorithm="HS256", headers={"kid": "new"})

    with pytest.raises(AuthenticationError):
        resolver.resolve(token)
    now[0] = 31
    assert resolver.resolve(token) is new_key
