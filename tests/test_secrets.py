import os
from pathlib import Path

import pytest

from modall.config import Settings
from modall.secrets.provider import (
    FixtureSecretProvider,
    MountedFileSecretProvider,
    SecretProviderError,
    SecretReference,
    build_secret_provider,
    validate_secret_reference,
)


def reference(
    provider: str = "fixture", name: str = "token", version: str = "v1"
) -> SecretReference:
    return SecretReference(provider=provider, external_reference=name, version=version)


def test_fixture_secret_lease_is_zeroed() -> None:
    provider = FixtureSecretProvider({("token", "v1"): b"secret-value"})

    with provider.retrieve(reference()) as value:
        leased = value
        assert bytes(value) == b"secret-value"

    assert bytes(leased) == b"\x00" * len(b"secret-value")


def test_fixture_provider_fails_without_reference_disclosure() -> None:
    provider = FixtureSecretProvider({})

    with pytest.raises(SecretProviderError, match="not found") as caught:
        provider.retrieve(reference(name="private-name"))
    assert "private-name" not in str(caught.value)

    with pytest.raises(SecretProviderError, match="mismatch"):
        provider.retrieve(reference(provider="mounted_file"))


@pytest.mark.parametrize(
    "invalid",
    [
        reference("mounted_file", "", "v1"),
        reference("mounted_file", "token", "v" * 129),
        reference("mounted_file", "r" * 128, "v" * 128),
        reference("mounted_file", "invalid/ref", "v1"),
        reference("fixture", "", "v1"),
        reference("fixture", "token", ""),
        reference("unknown", "token", "v1"),
    ],
)
def test_secret_reference_validation_rejects_unusable_metadata(
    invalid: SecretReference,
) -> None:
    with pytest.raises(SecretProviderError):
        validate_secret_reference(invalid)


def test_mounted_file_provider_reads_exact_version_and_zeroes(tmp_path: Path) -> None:
    filename = MountedFileSecretProvider.filename_for("api-token", "v2")
    (tmp_path / filename).write_bytes(b"mounted-secret\n")
    provider = MountedFileSecretProvider(tmp_path)

    with provider.retrieve(reference("mounted_file", "api-token", "v2")) as value:
        leased = value
        assert bytes(value) == b"mounted-secret\n"

    assert not any(leased)


@pytest.mark.parametrize(("name", "version"), [("../escape", "v1"), ("token", "../v1")])
def test_mounted_file_provider_rejects_traversal(tmp_path: Path, name: str, version: str) -> None:
    provider = MountedFileSecretProvider(tmp_path)

    with pytest.raises(SecretProviderError, match="invalid"):
        provider.retrieve(reference("mounted_file", name, version))


def test_mounted_file_provider_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"value")
    os.symlink(target, tmp_path / MountedFileSecretProvider.filename_for("link", "v1"))
    provider = MountedFileSecretProvider(tmp_path, max_bytes=4)

    with pytest.raises(SecretProviderError, match="failed"):
        provider.retrieve(reference("mounted_file", "link", "v1"))

    (tmp_path / MountedFileSecretProvider.filename_for("large", "v1")).write_bytes(b"12345")
    with pytest.raises(SecretProviderError, match="bound"):
        provider.retrieve(reference("mounted_file", "large", "v1"))


def test_mounted_file_mapping_is_injective(tmp_path: Path) -> None:
    first = MountedFileSecretProvider.filename_for("api", "token.v1")
    second = MountedFileSecretProvider.filename_for("api.token", "v1")
    assert first != second

    (tmp_path / first).write_bytes(b"first")
    (tmp_path / second).write_bytes(b"second")
    provider = MountedFileSecretProvider(tmp_path)
    with provider.retrieve(reference("mounted_file", "api", "token.v1")) as value:
        assert bytes(value) == b"first"
    with provider.retrieve(reference("mounted_file", "api.token", "v1")) as value:
        assert bytes(value) == b"second"


def test_mounted_file_provider_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    filename = MountedFileSecretProvider.filename_for("pipe", "v1")
    os.mkfifo(tmp_path / filename)

    with pytest.raises(SecretProviderError, match="regular file"):
        MountedFileSecretProvider(tmp_path).retrieve(reference("mounted_file", "pipe", "v1"))


def test_provider_errors_suppress_reference_bearing_causes(tmp_path: Path) -> None:
    with pytest.raises(SecretProviderError) as caught:
        MountedFileSecretProvider(tmp_path).retrieve(
            reference("mounted_file", "private-name", "v1")
        )
    assert caught.value.__cause__ is None


def test_mounted_file_provider_validates_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        MountedFileSecretProvider(tmp_path, max_bytes=0)

    file_path = tmp_path / "file"
    file_path.write_bytes(b"value")
    with pytest.raises(ValueError, match="directory"):
        MountedFileSecretProvider(file_path)


def test_provider_factory_separates_fixture_and_deployment_modes(tmp_path: Path) -> None:
    fixture = build_secret_provider(
        Settings(environment="test"), fixture_values={("token", "v1"): b"value"}
    )
    assert isinstance(fixture, FixtureSecretProvider)

    mounted = build_secret_provider(
        Settings(environment="test", secret_provider="mounted_file", secret_mount_root=tmp_path)
    )
    assert isinstance(mounted, MountedFileSecretProvider)
