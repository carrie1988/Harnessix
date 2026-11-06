"""evalkit命令行：导入Harbor Job、汇入判分生成报告、比较双判分、导出到Langfuse并读回。

离线优先：只有显式--allow-network后才读取Langfuse凭据环境变量。失败只输出固定代码，
不回显参数、响应或异常正文。解析器与判分器以“模块:类”指定，由被测系统一侧提供。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from evalkit.contracts import ArmTrials, EvalRunReport, RunPlan, TrialRecord
from evalkit.errors import EvalkitError
from evalkit.files import read_model, write_model
from evalkit.grading import Grader, build_report, consistency
from evalkit.harbor import HarborResultParser, import_harbor_job
from evalkit.langfuse import LangfuseExport, build_export, read_back, send

_MAX_PLAN_BYTES = 8 * 1024 * 1024
_MAX_REPORT_BYTES = 256 * 1024 * 1024


class _Parser(argparse.ArgumentParser):
    """参数出错时只输出固定提示，不回显可能含凭据的输入。"""

    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, f"{self.prog} 参数无效；使用 --help 查看格式。\n")


def _load(reference: str) -> Any:
    module, _, attribute = reference.partition(":")
    if not module or not attribute:
        raise EvalkitError("plugin_reference_invalid", "插件须写成“模块:类”")
    try:
        return getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError):
        raise EvalkitError("plugin_unavailable", "无法加载指定的解析器或判分器") from None


def _outside(output: Path, *sources: Path) -> Path:
    resolved = output.resolve()
    if any(resolved == s.resolve() or resolved.is_relative_to(s.resolve()) for s in sources):
        raise EvalkitError("output_conflicts_with_source", "输出不能写入或覆盖源证据")
    return output


def _import_harbor(args: argparse.Namespace) -> dict[str, Any]:
    plan = read_model(args.plan, RunPlan, _MAX_PLAN_BYTES)
    parsers: dict[str, HarborResultParser] = {}
    for reference in args.parser:
        parser = _load(reference)()
        parsers[parser.agent_name] = parser
    output = _outside(args.output, args.job_directory, args.plan)
    trials = import_harbor_job(
        args.job_directory,
        plan,
        args.arm,
        parsers,
        args.evidence_root,
        source_timezone=args.source_timezone,
    )
    write_model(
        output,
        ArmTrials(plan_digest=plan.digest, arm_id=args.arm, trials=trials),
        _MAX_REPORT_BYTES,
    )
    return {
        "reason": "imported",
        "arm_id": args.arm,
        "trials": len(trials),
        "missing": sum(t.status == "missing" for t in trials),
    }


def _report(args: argparse.Namespace) -> dict[str, Any]:
    plan = read_model(args.plan, RunPlan, _MAX_PLAN_BYTES)
    output = _outside(args.output, args.plan, *args.trials)
    trials: list[TrialRecord] = []
    for path in args.trials:
        arm_trials = read_model(path, ArmTrials, _MAX_REPORT_BYTES, require_private=True)
        if arm_trials.plan_digest != plan.digest:
            raise EvalkitError("trials_plan_mismatch", "试次文件不属于该计划")
        trials.extend(arm_trials.trials)
    declared = {grader.grader_id: grader for grader in plan.graders}
    graders: list[Grader] = []
    for reference in args.grader:
        grader_id, _, target = reference.partition("=")
        if grader_id not in declared:
            raise EvalkitError("grader_not_in_plan", "判分器不在计划内")
        graders.append(_load(target)(declared[grader_id]))
    report = build_report(
        plan, tuple(trials), tuple(graders), args.evidence_root, datetime.now(UTC)
    )
    write_model(output, report, _MAX_REPORT_BYTES)
    return {"reason": "reported", "summary": [s.model_dump() for s in report.summary]}


def _consistency(args: argparse.Namespace) -> dict[str, Any]:
    report = read_model(args.report, EvalRunReport, _MAX_REPORT_BYTES, require_private=True)
    output = _outside(args.output, args.report)
    result = consistency(report, args.first, args.second)
    write_model(output, result, _MAX_REPORT_BYTES)
    return {
        "reason": "compared",
        "compared": result.compared,
        "agreed": result.agreed,
        "disagreements": len(result.disagreements),
    }


def _submit(export: LangfuseExport, args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.langfuse_base_url:
        raise EvalkitError("langfuse_endpoint_missing", "联网导出需要显式指定Langfuse端点")
    credentials = {
        "base_url": args.langfuse_base_url,
        "public_key": os.environ.get(args.public_key_env, ""),
        "secret_key": os.environ.get(args.secret_key_env, ""),
    }
    send(export, **credentials)
    if not args.verify:
        return None
    readback = read_back(export, timeout_seconds=args.verify_timeout_seconds, **credentials)
    write_model(args.readback_report, readback, 65_536)
    return readback.model_dump(mode="json")


def _export(args: argparse.Namespace) -> dict[str, Any]:
    if args.verify and not (args.allow_network and args.readback_report):
        raise EvalkitError(
            "langfuse_readback_arguments_invalid", "读回核验需要显式联网开关和收据路径"
        )
    if args.verify and (
        not math.isfinite(args.verify_timeout_seconds) or not 0 < args.verify_timeout_seconds <= 600
    ):
        raise EvalkitError("langfuse_readback_budget_invalid", "Langfuse读回预算必须在0到600秒之间")
    output = _outside(args.output, args.report)
    if args.readback_report:
        _outside(args.readback_report, args.report, args.output)
    report = read_model(args.report, EvalRunReport, _MAX_REPORT_BYTES, require_private=True)
    export = build_export(report)
    write_model(output, export, _MAX_REPORT_BYTES)
    readback = _submit(export, args) if args.allow_network else None
    reason = "verified" if readback else "submitted" if args.allow_network else "prepared_offline"
    return {
        "reason": reason,
        "source_report_digest": export.source_report_digest,
        "trials": len(report.trials),
        **({"readback": readback} if readback else {}),
    }


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="python -m evalkit", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    harbor = commands.add_parser("import-harbor", allow_abbrev=False)
    harbor.add_argument("--plan", type=Path, required=True)
    harbor.add_argument("--arm", required=True)
    harbor.add_argument("--job-directory", type=Path, required=True)
    harbor.add_argument("--evidence-root", type=Path, required=True)
    harbor.add_argument("--parser", action="append", default=[])
    harbor.add_argument("--source-timezone")
    harbor.add_argument("--output", type=Path, required=True)
    harbor.set_defaults(run=_import_harbor)

    report = commands.add_parser("report", allow_abbrev=False)
    report.add_argument("--plan", type=Path, required=True)
    report.add_argument("--trials", type=Path, nargs="+", required=True)
    report.add_argument("--evidence-root", type=Path, required=True)
    report.add_argument("--grader", action="append", default=[], help="判分器ID=模块:类")
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(run=_report)

    compare = commands.add_parser("consistency", allow_abbrev=False)
    compare.add_argument("--report", type=Path, required=True)
    compare.add_argument("--first", required=True)
    compare.add_argument("--second", required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(run=_consistency)

    export = commands.add_parser("export", allow_abbrev=False)
    export.add_argument("--report", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--allow-network", action="store_true")
    export.add_argument("--langfuse-base-url")
    export.add_argument("--public-key-env", default="LANGFUSE_PUBLIC_KEY")
    export.add_argument("--secret-key-env", default="LANGFUSE_SECRET_KEY")
    export.add_argument("--verify", action="store_true")
    export.add_argument("--verify-timeout-seconds", type=float, default=120.0)
    export.add_argument("--readback-report", type=Path)
    export.set_defaults(run=_export)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _arguments(argv)
    run: Callable[[argparse.Namespace], dict[str, Any]] = args.run
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(args), ensure_ascii=False))
    except EvalkitError as error:
        print(json.dumps({"reason": error.code}))
        raise SystemExit(1) from None
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"reason": "evalkit_input_invalid"}))
        raise SystemExit(1) from None
    finally:
        logging.disable(previous)
