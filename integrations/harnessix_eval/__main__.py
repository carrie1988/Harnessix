"""python -m integrations.harnessix_eval from-suite：Suite报告 → evalkit运行报告（0600）。

之后用 python -m evalkit export 生成Langfuse投影；Harbor导入用 python -m evalkit import-harbor
--parser integrations.harnessix_eval.harbor_parser:HarnessixResultParser。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evalkit.errors import EvalkitError
from evalkit.files import write_model

from harnessix.agent.errors import KernelError
from harnessix.evals.report import read_eval_suite_report
from integrations.harnessix_eval.suite import suite_run_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m integrations.harnessix_eval")
    commands = parser.add_subparsers(dest="command", required=True)
    suite = commands.add_parser("from-suite")
    suite.add_argument("--suite-report", type=Path, required=True)
    suite.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.suite_report.resolve():
            raise EvalkitError("output_conflicts_with_source", "输出不能覆盖源报告")
        report = suite_run_report(read_eval_suite_report(args.suite_report))
        write_model(args.output, report, 256 * 1024 * 1024)
        summary = [s.model_dump() for s in report.summary]
        print(json.dumps({"reason": "converted", "summary": summary}))
    except (EvalkitError, KernelError) as error:
        print(json.dumps({"reason": error.code}))
        raise SystemExit(1) from None
    except (OSError, ValueError):
        print(json.dumps({"reason": "suite_report_invalid"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main(sys.argv[1:])
