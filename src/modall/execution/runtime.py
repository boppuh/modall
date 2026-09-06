"""Shared construction of secret-backed execution keyrings."""

from collections.abc import Sequence

from modall.config import Settings
from modall.execution.types import HmacKeyVersion
from modall.secrets.provider import SecretProvider, SecretReference, build_secret_provider

CONFIRMATION_KEY_REFERENCE = "system-confirmation-hmac"
IDEMPOTENCY_KEY_REFERENCE = "system-idempotency-hmac"


def build_execution_keyrings(
    settings: Settings,
) -> tuple[tuple[HmacKeyVersion, ...], tuple[HmacKeyVersion, ...]]:
    """Load API/worker keyrings from the same configured secret source."""

    fixture_values: dict[tuple[str, str], bytes] | None = None
    if settings.environment in {"local", "test"} and settings.secret_provider == "fixture":
        fixture_values = {
            (CONFIRMATION_KEY_REFERENCE, version): (
                f"local-confirmation-{version}-key-material".encode()
            )
            for version in settings.confirmation_hmac_key_versions
        }
        fixture_values.update(
            {
                (IDEMPOTENCY_KEY_REFERENCE, version): (
                    f"local-idempotency-{version}-key-material".encode()
                )
                for version in settings.idempotency_hmac_key_versions
            }
        )
    provider = build_secret_provider(settings, fixture_values=fixture_values)
    return (
        load_keyring(
            provider,
            settings.secret_provider,
            CONFIRMATION_KEY_REFERENCE,
            settings.confirmation_hmac_key_versions,
        ),
        load_keyring(
            provider,
            settings.secret_provider,
            IDEMPOTENCY_KEY_REFERENCE,
            settings.idempotency_hmac_key_versions,
        ),
    )


def load_keyring(
    provider: SecretProvider,
    provider_name: str,
    reference: str,
    versions: Sequence[str],
) -> tuple[HmacKeyVersion, ...]:
    """Read and immediately copy a bounded versioned HMAC keyring."""

    keys: list[HmacKeyVersion] = []
    for version in versions:
        with provider.retrieve(
            SecretReference(
                provider=provider_name,
                external_reference=reference,
                version=version,
            )
        ) as secret:
            if len(secret) < 32:
                raise ValueError("HMAC key material is too short")
            keys.append(HmacKeyVersion(version=version, secret=bytes(secret)))
    return tuple(keys)
