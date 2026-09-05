"""Secret-provider contracts and alpha implementations."""

import base64
import os
import re
import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

from modall.config import Settings


class SecretProviderError(Exception):
    """Safe provider failure that never contains a reference or secret value."""


@dataclass(frozen=True, slots=True)
class SecretReference:
    provider: str
    external_reference: str
    version: str


class SecretLease(AbstractContextManager[bytearray]):
    """Best-effort zeroing for a transient mutable credential buffer."""

    def __init__(self, value: bytes) -> None:
        if not value:
            raise SecretProviderError("secret value is empty")
        self._value = bytearray(value)

    def __enter__(self) -> bytearray:
        return self._value

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for index in range(len(self._value)):
            self._value[index] = 0


class SecretProvider(Protocol):
    def retrieve(self, reference: SecretReference) -> SecretLease: ...


class FixtureSecretProvider:
    """In-memory provider restricted to tests and local fixtures."""

    def __init__(self, values: dict[tuple[str, str], bytes]) -> None:
        self._values = values

    def retrieve(self, reference: SecretReference) -> SecretLease:
        if reference.provider != "fixture":
            raise SecretProviderError("secret provider mismatch")
        try:
            value = self._values[(reference.external_reference, reference.version)]
        except KeyError:
            raise SecretProviderError("secret reference not found") from None
        return SecretLease(value)


class MountedFileSecretProvider:
    """Read a deployment secret projected beneath one non-traversable mount root."""

    _REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

    def __init__(self, root: Path, *, max_bytes: int = 65_536) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._root = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("secret mount root must be a directory")
        self._max_bytes = max_bytes

    @classmethod
    def filename_for(cls, external_reference: str, version: str) -> str:
        """Map a reference/version pair to an injective, path-safe filename."""

        if (
            cls._REFERENCE.fullmatch(external_reference) is None
            or cls._REFERENCE.fullmatch(version) is None
        ):
            raise SecretProviderError("invalid secret reference")

        def encode(component: str) -> str:
            return base64.urlsafe_b64encode(component.encode()).rstrip(b"=").decode("ascii")

        return f"{encode(external_reference)}.{encode(version)}"

    def retrieve(self, reference: SecretReference) -> SecretLease:
        if reference.provider != "mounted_file":
            raise SecretProviderError("secret provider mismatch")
        filename = self.filename_for(reference.external_reference, reference.version)

        directory_fd: int | None = None
        try:
            directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SecretProviderError("secret reference is not a regular file")
                if metadata.st_size > self._max_bytes:
                    raise SecretProviderError("secret value exceeds configured bound")
                chunks: list[bytes] = []
                remaining = self._max_bytes + 1
                while remaining > 0:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                value = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError:
            raise SecretProviderError("secret retrieval failed") from None
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

        if len(value) > self._max_bytes:
            raise SecretProviderError("secret value exceeds configured bound")
        return SecretLease(value)


def build_secret_provider(
    settings: Settings,
    *,
    fixture_values: dict[tuple[str, str], bytes] | None = None,
) -> SecretProvider:
    """Select a provider without permitting fixtures in a deployed process."""

    if settings.secret_provider == "mounted_file":
        return MountedFileSecretProvider(settings.secret_mount_root)
    if settings.environment not in {"local", "test"}:
        raise ValueError("fixture secrets are restricted to local and test environments")
    return FixtureSecretProvider(fixture_values or {})
