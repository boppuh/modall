"""Explicit operational controls for restore fencing and bounded maintenance."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from sqlalchemy import func, select

from modall.config import Settings, get_settings
from modall.execution.runtime import build_execution_keyrings, build_execution_limits
from modall.execution.service import ExecutionService
from modall.execution.types import RunStatus, SystemExecutionAuthority
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.persistence.models import Run, SystemExecutionState
from modall.worker.main import _run_maintenance, build_execution_runtime

_ACTIVE = (
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.SESSION_FENCED.value,
    RunStatus.DISPATCH_FENCED.value,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modall-ops")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show payload-free execution posture")
    enter = commands.add_parser("restore-enter", help="Fence all dispatch before a restore")
    enter.add_argument("--confirm", required=True, choices=("RESTORE",))
    reconcile = commands.add_parser(
        "restore-reconcile", help="Terminalize one bounded batch of restored active runs"
    )
    reconcile.add_argument("--batch-size", type=int, default=100)
    clear = commands.add_parser("restore-clear", help="Clear quarantine after reconciliation")
    clear.add_argument("--confirm", required=True, choices=("CLEAR",))
    commands.add_parser("maintenance", help="Run one bounded retention-maintenance pass")
    return parser


async def execute(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    engine = create_engine(async_database_url(str(settings.database_url)))
    factory = create_session_factory(engine)

    try:
        if args.command == "maintenance":
            _, execution_factory = build_execution_runtime(settings, factory)
            await _run_maintenance(
                settings=settings,
                session_factory=factory,
                execution_service_factory=execution_factory,
            )
            return {"operation": "maintenance", "status": "completed"}
        async with transaction(factory) as session:
            if args.command == "status":
                state = await session.get(SystemExecutionState, 1)
                active = await session.scalar(
                    select(func.count()).select_from(Run).where(Run.status.in_(_ACTIVE))
                )
                return {
                    "dispatch_quarantined": bool(state and state.dispatch_quarantined),
                    "execution_epoch": state.execution_epoch if state else None,
                    "active_runs": active or 0,
                }
            confirmation, idempotency = build_execution_keyrings(settings)
            execution = ExecutionService(
                session,
                confirmation_keys=confirmation,
                idempotency_keys=idempotency,
                limits=build_execution_limits(settings),
                system_authority=SystemExecutionAuthority(),
            )
            if args.command == "restore-enter":
                epoch = await execution.enter_restore_quarantine()
                return {"operation": "restore-enter", "execution_epoch": epoch}
            if args.command == "restore-reconcile":
                reconciled = await execution.reconcile_restore_quarantine(
                    batch_size=args.batch_size
                )
                return {"operation": "restore-reconcile", "reconciled": reconciled}
            if args.command == "restore-clear":
                epoch = await execution.clear_restore_quarantine()
                return {"operation": "restore-clear", "execution_epoch": epoch}
        raise RuntimeError("unsupported command")
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    result = asyncio.run(execute(get_settings(), args))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
