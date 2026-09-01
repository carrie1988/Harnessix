from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from harnessix.bootstrap import build_service
from harnessix.observability import configure_logging
from harnessix.settings import Settings
from harnessix.worker import ActionWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harnessix Action Plane")
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve", help="启动 HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--database-path")
    serve.add_argument("--execution-mode", choices=("inline", "queued"))
    worker = subcommands.add_parser("worker", help="启动独立 Action Worker")
    worker.add_argument("--database-path")
    worker.add_argument("--once", action="store_true", help="最多执行一个 READY Action 后退出")
    return parser


async def _run_worker(settings: Settings, *, once: bool) -> None:
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    service = build_service(settings, worker_id=worker_id)
    await service.initialize()
    worker = ActionWorker(
        service,
        poll_seconds=settings.worker_poll_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        recovery_interval_seconds=settings.recovery_interval_seconds,
    )
    logging.info("Harnessix Worker 已启动：%s", worker_id)
    try:
        if once:
            await worker.run_once()
        else:
            await worker.run_forever()
    finally:
        await service.close()


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    settings = Settings.from_environment()
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    if arguments.command == "serve":
        host = arguments.host or settings.host
        port = arguments.port or settings.port
        if arguments.database_path:
            os.environ["HARNESSIX_DATABASE_PATH"] = arguments.database_path
        if arguments.execution_mode:
            os.environ["HARNESSIX_EXECUTION_MODE"] = arguments.execution_mode
        uvicorn.run(
            "harnessix.api.app:app",
            host=host,
            port=port,
            factory=False,
            log_config=None,
        )
    elif arguments.command == "worker":
        if arguments.database_path:
            settings = replace(settings, database_path=Path(arguments.database_path))
        asyncio.run(_run_worker(settings, once=arguments.once))
