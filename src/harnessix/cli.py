from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from harnessix.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harnessix Action Plane")
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve", help="启动 HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--database-path")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    settings = Settings.from_environment()
    if arguments.command == "serve":
        host = arguments.host or settings.host
        port = arguments.port or settings.port
        if arguments.database_path:
            import os

            os.environ["HARNESSIX_DATABASE_PATH"] = arguments.database_path
        uvicorn.run("harnessix.api.app:app", host=host, port=port, factory=False)
