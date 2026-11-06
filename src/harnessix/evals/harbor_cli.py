"""显式将本地Harbor运行转换为私有原生报告；不联网、不读取模型凭据。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.evals.cli_support import SafeEvalParser
from harnessix.evals.harbor_import import safe_import_harbor_job
from harnessix.evals.report import write_harbor_eval_report


def main(argv: Sequence[str] | None = None) -> None:
    """读冻结锁和结果并写0600报告；源证据目录内禁止写入，异常正文不回显。"""
    parser = SafeEvalParser(prog="harnessix coding-eval-import-harbor", allow_abbrev=False)
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-timezone")
    args = parser.parse_args(argv)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args.output.resolve().is_relative_to(args.job_directory.resolve()):
            raise KernelError("harbor_source_conflict", "原生报告必须写到Harbor源目录外")
        report = safe_import_harbor_job(
            args.job_directory,
            source_timezone=args.source_timezone,
        )
        write_harbor_eval_report(args.output, report)
        print(
            json.dumps(
                {
                    "reason": "imported",
                    "job_id": str(report.job_id),
                    "summary": report.summary.model_dump(mode="json"),
                }
            )
        )
    except KernelError as error:
        print(json.dumps({"reason": error.code}))
        raise SystemExit(1) from None
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"reason": "harbor_report_invalid"}))
        raise SystemExit(1) from None
    finally:
        logging.disable(previous_logging)
