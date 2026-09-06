"""Hard-bounded JSON Schema evaluation for untrusted capability schemas."""

import asyncio
import multiprocessing
import resource
import sys
from contextlib import suppress
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from threading import BoundedSemaphore
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

_PROCESS_CONTEXT = multiprocessing.get_context("spawn")
_PROCESS_PERMITS = BoundedSemaphore(4)


class SchemaValidationResult(StrEnum):
    VALID = "valid"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_SCHEMA = "invalid_schema"
    FAILED = "failed"


async def validate_schema_arguments(
    arguments: dict[str, object],
    schema: dict[str, object],
    *,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> SchemaValidationResult:
    """Evaluate jsonschema in a killable, concurrency-limited child process."""

    permit_acquired = False
    receiver: Connection | None = None
    sender: Connection | None = None
    process: SpawnProcess | None = None
    result = SchemaValidationResult.FAILED
    try:
        async with asyncio.timeout(timeout_seconds):
            while not _PROCESS_PERMITS.acquire(blocking=False):
                await asyncio.sleep(0.005)
            permit_acquired = True
            receiver, sender = _PROCESS_CONTEXT.Pipe(duplex=False)
            process = _PROCESS_CONTEXT.Process(
                target=_schema_validation_process,
                args=(arguments, schema, sender, memory_limit_bytes),
                daemon=True,
            )
            process.start()
            sender.close()
            sender = None
            received = await asyncio.to_thread(receiver.recv)
            result = SchemaValidationResult(cast(str, received))
    except TimeoutError:
        result = SchemaValidationResult.FAILED
    except asyncio.CancelledError:
        raise
    except Exception:
        result = SchemaValidationResult.FAILED
    finally:
        cleanup = asyncio.create_task(_close_process(process, receiver, sender))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if permit_acquired:
            _PROCESS_PERMITS.release()
        if cancelled:
            raise asyncio.CancelledError
    return result


def _schema_validation_process(
    arguments: dict[str, object],
    schema: dict[str, object],
    sender: Connection,
    memory_limit_bytes: int,
) -> None:
    try:
        try:
            _apply_validation_memory_limit(memory_limit_bytes)
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
        except ValidationError:
            result = SchemaValidationResult.INVALID_ARGUMENTS
        except SchemaError:
            result = SchemaValidationResult.INVALID_SCHEMA
        except BaseException:
            result = SchemaValidationResult.FAILED
        else:
            result = SchemaValidationResult.VALID
        with suppress(Exception):
            sender.send(result.value)
    finally:
        with suppress(Exception):
            sender.close()


def _apply_validation_memory_limit(memory_limit_bytes: int) -> None:
    """Install the deployment-platform address-space ceiling before validation."""

    if sys.platform != "linux":
        return
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))


async def _close_process(
    process: SpawnProcess | None,
    receiver: Connection | None,
    sender: Connection | None,
) -> None:
    if sender is not None:
        with suppress(Exception):
            sender.close()
    if process is not None and process.pid is not None:
        if process.is_alive():
            with suppress(Exception):
                await asyncio.to_thread(process.join, 0.25)
        if process.is_alive():
            with suppress(Exception):
                process.terminate()
            with suppress(Exception):
                await asyncio.to_thread(process.join, 0.25)
        if process.is_alive():
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await asyncio.to_thread(process.join, 0.25)
        if not process.is_alive():
            with suppress(Exception):
                process.close()
    if receiver is not None:
        with suppress(Exception):
            receiver.close()
