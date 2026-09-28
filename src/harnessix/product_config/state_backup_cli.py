"""正式停机备份与验真命令；只输出低敏摘要，不显示Key、正文或内部异常。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup import backup_product_state, verify_product_backup


def state_main(argv: Sequence[str] | None = None) -> None:
    """单一产品的离线备份入口，不提供清理、跨机迁移或未知历史重签功能。"""
    parser = argparse.ArgumentParser(prog="harnessix state", description="产品状态停机备份与验真")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation, help_text in (("backup", "创建完整可信备份"), ("verify", "验证原本机备份")):
        command = commands.add_parser(operation, help=help_text)
        command.add_argument("--state-directory", required=True, type=Path)
        command.add_argument("--backup-directory", required=True, type=Path)
        command.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    handler = backup_product_state if args.operation == "backup" else verify_product_backup
    try:
        manifest = asyncio.run(
            handler(
                args.state_directory,
                args.backup_directory,
                budget_seconds=args.timeout,
            )
        )
        print(
            json.dumps(
                {
                    "status": "backed_up" if args.operation == "backup" else "verified",
                    "backup_id": str(manifest.backup_id),
                    "files": len(manifest.files),
                    "size_bytes": sum(entry.size_bytes for entry in manifest.files),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    except KernelError as error:
        print(
            json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception:
        print(
            json.dumps(
                {"code": "product_backup_internal_failure", "message": "产品备份操作失败"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
