"""解析顶层命令并分派服务、Worker、Eval和配置子命令。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from harnessix.bootstrap import build_service
from harnessix.licensing import render_license_notice
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
    subcommands.add_parser("model-smoke", help="运行显式启用的固定场景模型验收")
    subcommands.add_parser(
        "coding-eval-campaign", help="运行显式启用的固定历史任务真实模型Campaign"
    )
    subcommands.add_parser("agent", help="通过stdio App Server运行薄Agent CLI")
    subcommands.add_parser("agent-server", help="按产品配置运行stdio App Server")
    subcommands.add_parser("config", help="诊断或迁移产品配置")
    subcommands.add_parser("license", help="显示社区许可证、源代码和商业许可信息")
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
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "model-smoke":
        from harnessix.smoke.cli import main as smoke_main

        smoke_main(args[1:])
        return
    if args and args[0] == "coding-eval-campaign":
        from harnessix.evals.campaign_cli import main as campaign_main

        campaign_main(args[1:])
        return
    if args and args[0] == "agent":
        from harnessix.agent_cli import main as agent_main

        agent_main(args[1:])
        return
    if args and args[0] == "agent-server":
        from harnessix.product_config.cli import agent_server_main

        agent_server_main(args[1:])
        return
    if args and args[0] == "config":
        from harnessix.product_config.cli import config_main

        config_main(args[1:])
        return
    arguments = _parser().parse_args(args)
    if arguments.command == "license":
        print(render_license_notice())
        return
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
