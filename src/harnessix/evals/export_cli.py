"""离线优先的Suite导出CLI；显式网络开关之后才读取导出凭据。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.evals.cli_support import SafeEvalParser as _SafeParser
from harnessix.evals.langfuse_contracts import (
    LangfuseExport,
    LangfuseReadbackReport,
    LangfuseSuiteExport,
)
from harnessix.evals.langfuse_harbor_projection import build_langfuse_harbor_export
from harnessix.evals.langfuse_projection import build_langfuse_suite_export
from harnessix.evals.langfuse_readback import verify_langfuse_export
from harnessix.evals.langfuse_transport import send_langfuse_suite_export
from harnessix.evals.report import (
    read_eval_suite_report,
    read_harbor_eval_report,
    write_langfuse_readback,
    write_langfuse_suite_export,
)


def _submit(
    export: LangfuseExport, arguments: argparse.Namespace, source: Path, output: Path
) -> LangfuseReadbackReport | None:
    if not arguments.allow_network:
        return None
    if not arguments.langfuse_base_url:
        raise KernelError("langfuse_endpoint_missing", "联网导出需要显式指定Langfuse端点")
    readback = (
        Path(arguments.readback_report)
        if arguments.readback_report
        else output.with_suffix(".verified.json")
    )
    if arguments.verify and readback.resolve() in {source.resolve(), output.resolve()}:
        raise KernelError("langfuse_export_source_conflict", "核验收据不能覆盖源报告或导出文件")
    public_key = os.environ.get(arguments.public_key_env, "")
    secret_key = os.environ.get(arguments.secret_key_env, "")
    send_langfuse_suite_export(
        export, base_url=arguments.langfuse_base_url, public_key=public_key, secret_key=secret_key
    )
    if not arguments.verify:
        return None
    result = verify_langfuse_export(
        export,
        base_url=arguments.langfuse_base_url,
        timeout_seconds=arguments.verify_timeout_seconds,
        public_key=public_key,
        secret_key=secret_key,
    )
    write_langfuse_readback(readback, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    """只读原生评测并生成0600文件；可显式发送、读回并保存私有核验收据。"""

    parser = _SafeParser(prog="harnessix coding-eval-export", allow_abbrev=False)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--suite-report")
    sources.add_argument("--harbor-report")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--langfuse-base-url")
    parser.add_argument("--public-key-env", default="LANGFUSE_PUBLIC_KEY")
    parser.add_argument("--secret-key-env", default="LANGFUSE_SECRET_KEY")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verify-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--readback-report")
    arguments = parser.parse_args(argv)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if (arguments.verify and not arguments.allow_network) or (
            arguments.readback_report and not arguments.verify
        ):
            raise KernelError(
                "langfuse_readback_arguments_invalid", "读回核验需要--verify及显式联网开关"
            )
        if arguments.verify and (
            not math.isfinite(arguments.verify_timeout_seconds)
            or not 0 < arguments.verify_timeout_seconds <= 600
        ):
            raise KernelError(
                "langfuse_readback_budget_invalid", "Langfuse读回预算必须在0到600秒之间"
            )
        source = Path(arguments.suite_report or arguments.harbor_report)
        output = Path(arguments.output)
        if source.resolve() == output.resolve():
            raise KernelError("langfuse_export_source_conflict", "导出文件不能覆盖源Suite报告")
        export = (
            build_langfuse_harbor_export(read_harbor_eval_report(source))
            if arguments.harbor_report
            else build_langfuse_suite_export(read_eval_suite_report(source))
        )
        write_langfuse_suite_export(output, export)
        verification = _submit(export, arguments, source, output)
        print(
            json.dumps(
                {
                    "reason": "verified"
                    if verification
                    else "submitted"
                    if arguments.allow_network
                    else "prepared_offline",
                    **(
                        {"suite_id": str(export.suite_id)}
                        if isinstance(export, LangfuseSuiteExport)
                        else {"job_id": str(export.job_id)}
                    ),
                    "scheduled_trials": export.summary.scheduled_trials,
                    "source_report_sha256": export.source_report_sha256,
                    **({"readback": verification.model_dump(mode="json")} if verification else {}),
                }
            )
        )
    except KernelError as error:
        print(json.dumps({"reason": error.code}))
        raise SystemExit(1) from None
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"reason": "langfuse_export_invalid"}))
        raise SystemExit(1) from None
    finally:
        logging.disable(previous_logging)
