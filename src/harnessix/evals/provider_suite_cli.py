"""受控真实Provider Suite的显式禁网CLI。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from typing import NoReturn

from harnessix.agent.errors import KernelError
from harnessix.evals.cli_config import read_private_eval_config
from harnessix.evals.provider_suite_contracts import (
    CodingEvalProviderSuiteRunConfig,
    CodingEvalProviderSuiteRunReport,
)
from harnessix.evals.provider_suite_execution import run_task_pack_provider_suite
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunReport

_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, "coding-eval-suite 参数无效；使用 --help 查看格式。\n")


def _read_config(path: str) -> CodingEvalProviderSuiteRunConfig:
    return read_private_eval_config(
        path,
        CodingEvalProviderSuiteRunConfig,
        max_bytes=_MAX_CONFIG_BYTES,
    )


def _public_report(result: CodingEvalSuiteRunReport) -> CodingEvalProviderSuiteRunReport:
    return CodingEvalProviderSuiteRunReport(
        reason=result.reason,
        suite_id=result.suite_id,
        scheduled_cases=result.scheduled_cases,
        completed_cases=result.completed_cases,
        current_case_id=result.current_case_id,
        report_published=result.report_published,
        known_cost_currency=result.known_cost_currency,
        known_cost_amount=result.known_cost_amount,
    )


def _identified_failure(
    config: CodingEvalProviderSuiteRunConfig,
    reason: str,
) -> CodingEvalProviderSuiteRunReport:
    return CodingEvalProviderSuiteRunReport.model_validate(
        {
            "reason": reason,
            "suite_id": config.suite.plan.suite_id,
            "scheduled_cases": len(config.suite.plan.cases),
            "completed_cases": 0,
            "known_cost_currency": config.suite.fee_stop_currency,
            "known_cost_amount": "0",
        }
    )


def main(argv: Sequence[str]) -> None:
    parser = _SafeParser(
        prog="harnessix coding-eval-suite",
        description="固定Task Pack真实Provider Suite；默认不读取配置、凭据或访问网络",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, help="0600 UTF-8 JSON配置（仅凭据环境引用）")
    parser.add_argument("--allow-network", action="store_true", help="显式允许真实Provider请求")
    parser.add_argument("--resume", action="store_true", help="显式恢复已停止的同一Suite")
    arguments = parser.parse_args(argv)
    if not arguments.allow_network:
        print(CodingEvalProviderSuiteRunReport(reason="network_not_enabled").model_dump_json())
        raise SystemExit(2)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    exit_code = 1
    try:
        try:
            config = _read_config(arguments.config)
        except (OSError, ValueError, RecursionError):
            report = CodingEvalProviderSuiteRunReport(reason="configuration_invalid")
            exit_code = 2
        else:
            try:
                result = asyncio.run(
                    run_task_pack_provider_suite(
                        config,
                        allow_network=True,
                        resume=arguments.resume,
                    )
                )
                report = _public_report(result)
                exit_code = 0 if result.reason == "completed" else 1
            except ImportError:
                report = CodingEvalProviderSuiteRunReport(reason="dependency_missing")
            except ValueError:
                report = CodingEvalProviderSuiteRunReport(reason="configuration_invalid")
                exit_code = 2
            except KeyboardInterrupt:
                report = _identified_failure(config, "cancelled")
                exit_code = 130
            except KernelError:
                report = _identified_failure(config, "runtime_failed")
            except Exception:
                report = CodingEvalProviderSuiteRunReport(reason="internal_error")
    finally:
        logging.disable(previous_logging)
    print(report.model_dump_json())
    raise SystemExit(exit_code)
