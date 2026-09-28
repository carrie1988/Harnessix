"""正式停机备份、验真、完整恢复及显式结算；只输出低敏摘要和固定错误。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
from harnessix.product_config.state_restore import (
    recover_product_state_restore,
    restore_product_state,
)


def state_main(argv: Sequence[str] | None = None) -> None:
    """单一产品的离线状态入口，不提供清理、跨机迁移或未知历史重签功能。"""
    parser = argparse.ArgumentParser(
        prog="harnessix state", description="产品状态停机备份、验真与恢复"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation, help_text in (("backup", "创建完整可信备份"), ("verify", "验证原本机备份")):
        command = commands.add_parser(operation, help=help_text)
        command.add_argument("--state-directory", required=True, type=Path)
        command.add_argument("--backup-directory", required=True, type=Path)
        command.add_argument("--timeout", type=float, default=120.0)
    restore = commands.add_parser("restore", help="明确替换完整产品状态并保留原目录")
    restore.add_argument("--state-directory", required=True, type=Path)
    restore.add_argument("--backup-directory", required=True, type=Path)
    restore.add_argument("--restore-id", required=True, type=_uuid)
    restore.add_argument("--confirm-backup", required=True, type=_uuid)
    restore.add_argument("--timeout", type=float, default=120.0)
    recover = commands.add_parser("recover", help="明确完成或回退原未决恢复")
    recover.add_argument("--state-directory", required=True, type=Path)
    recover.add_argument("--confirm-restore", required=True, type=_uuid)
    recover.add_argument("--mode", choices=("complete", "rollback"), required=True)
    recover.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(asyncio.run(_execute(args)), ensure_ascii=False, sort_keys=True))
    except KernelError as error:
        print(
            json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception:
        code = (
            "product_backup_internal_failure"
            if args.operation in {"backup", "verify"}
            else "product_restore_internal_failure"
        )
        print(
            json.dumps({"code": code, "message": "产品状态操作失败"}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("恢复身份必须是UUID") from None


async def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.operation == "restore":
        result = await restore_product_state(
            args.state_directory,
            args.backup_directory,
            restore_id=args.restore_id,
            confirm_backup_id=args.confirm_backup,
            budget_seconds=args.timeout,
        )
        return result.model_dump(mode="json")
    if args.operation == "recover":
        result = await recover_product_state_restore(
            args.state_directory,
            confirm_restore_id=args.confirm_restore,
            mode=args.mode,
            budget_seconds=args.timeout,
        )
        return result.model_dump(mode="json")
    handler = backup_product_state if args.operation == "backup" else verify_product_backup
    manifest = await handler(
        args.state_directory, args.backup_directory, budget_seconds=args.timeout
    )
    return {
        "status": "backed_up" if args.operation == "backup" else "verified",
        "backup_id": str(manifest.backup_id),
        "files": len(manifest.files),
        "size_bytes": sum(entry.size_bytes for entry in manifest.files),
    }
