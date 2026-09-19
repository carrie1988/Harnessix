"""产品配置：解析用户命令并调用对应应用服务，不承载领域规则。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot
from harnessix.product_config.migration import migrate_product_config_file
from harnessix.product_config.runtime import (
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)
from harnessix.product_config.server import run_product_stdio
from harnessix.product_config.store import SQLiteProductConfigStore


def _config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnessix config", description="产品配置管理")
    commands = parser.add_subparsers(dest="operation", required=True)
    diagnose = commands.add_parser("diagnose", help="离线诊断Provider/Profile/Secret")
    diagnose.add_argument("--config", required=True)
    diagnose.add_argument("--profile")
    diagnose.add_argument("--state-database")
    migrate = commands.add_parser("migrate", help="以源摘要CAS迁移v1配置")
    migrate.add_argument("--config", required=True)
    migrate.add_argument("--expected-source-sha256", required=True)
    migrate.add_argument("--state-database")
    return parser


def _server_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix agent-server", description="运行已配置的stdio Agent Server"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--action-config")
    parser.add_argument("--profile")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--expected-active-sha256")
    parser.add_argument("--expected-active-profile")
    parser.add_argument("--expected-active-action-sha256")
    parser.add_argument("--git-executable")
    return parser


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _exit_with_error(code: str, message: str, *, retryable: bool = False) -> None:
    print(_json({"code": code, "message": message, "retryable": retryable}), file=sys.stderr)
    raise SystemExit(2) from None


def config_main(argv: Sequence[str] | None = None) -> None:
    args = _config_parser().parse_args(argv)
    try:
        if args.operation == "diagnose":
            loaded = load_product_config(args.config)
            if not isinstance(loaded, ProductConfigSnapshot):
                raise KernelError("product_config_migration_required", "产品配置必须迁移到v2")
            selection = select_profile(loaded, args.profile)
            report = diagnose_configuration(
                loaded,
                selection,
                environment_secret_provider(loaded.config),
            )
            if args.state_database:
                with SQLiteProductConfigStore(args.state_database) as store:
                    store.save_snapshot(loaded)
            print(_json(report))
            raise SystemExit(0 if report.ready else 2)
        receipt = migrate_product_config_file(
            args.config,
            expected_source_sha256=args.expected_source_sha256,
        )
        if args.state_database:
            loaded = load_product_config(args.config)
            assert isinstance(loaded, ProductConfigSnapshot)
            with SQLiteProductConfigStore(args.state_database) as store:
                store.record_migration(receipt, loaded)
        print(_json(receipt))
    except KernelError as error:
        _exit_with_error(error.code, error.message, retryable=error.retryable)
    except Exception:
        _exit_with_error("product_internal_failure", "产品配置操作发生内部错误")


def agent_server_main(argv: Sequence[str] | None = None) -> None:
    args = _server_parser().parse_args(argv)
    try:
        asyncio.run(
            run_product_stdio(
                config_path=args.config,
                action_config_path=args.action_config,
                profile_id=args.profile,
                workspace=args.workspace,
                state_directory=args.state_directory,
                input_stream=sys.stdin.buffer,
                output_stream=sys.stdout.buffer,
                expected_active_sha256=args.expected_active_sha256,
                expected_active_profile=args.expected_active_profile,
                expected_active_action_sha256=args.expected_active_action_sha256,
                git_executable=args.git_executable,
            )
        )
    except KernelError as error:
        _exit_with_error(error.code, error.message, retryable=error.retryable)
    except Exception:
        _exit_with_error("product_internal_failure", "产品运行时发生内部错误")
