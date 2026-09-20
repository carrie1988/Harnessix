"""真实Coding Eval Campaign的显式禁网CLI。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from typing import NoReturn

from harnessix.agent.errors import KernelError
from harnessix.evals.campaign_execution import run_coding_eval_campaign
from harnessix.evals.campaign_execution_contracts import (
    CodingEvalCampaignRunConfig,
    CodingEvalCampaignRunReport,
)
from harnessix.evals.cli_config import read_private_eval_config

_MAX_CONFIG_BYTES = 512 * 1024


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, "coding-eval-campaign 参数无效；使用 --help 查看格式。\n")


def _read_config(path: str) -> CodingEvalCampaignRunConfig:
    return read_private_eval_config(
        path,
        CodingEvalCampaignRunConfig,
        max_bytes=_MAX_CONFIG_BYTES,
    )


def main(argv: Sequence[str]) -> None:
    parser = _SafeParser(
        prog="harnessix coding-eval-campaign",
        description="固定历史任务真实模型Campaign；默认不读取配置、凭据或访问网络",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, help="0600 UTF-8 JSON配置（仅凭据环境引用）")
    parser.add_argument("--allow-network", action="store_true", help="显式允许真实Provider请求")
    arguments = parser.parse_args(argv)
    if not arguments.allow_network:
        print(CodingEvalCampaignRunReport(reason="network_not_enabled").model_dump_json())
        raise SystemExit(2)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    exit_code = 1
    try:
        try:
            config = _read_config(arguments.config)
        except (OSError, ValueError, RecursionError):
            report = CodingEvalCampaignRunReport(reason="configuration_invalid")
            exit_code = 2
        else:
            try:
                report = asyncio.run(run_coding_eval_campaign(config, allow_network=True))
                exit_code = 0 if report.reason == "completed" else 1
            except ImportError:
                report = CodingEvalCampaignRunReport(reason="dependency_missing")
            except ValueError:
                report = CodingEvalCampaignRunReport(reason="configuration_invalid")
                exit_code = 2
            except KeyboardInterrupt:
                report = CodingEvalCampaignRunReport(
                    reason="cancelled",
                    campaign_id=config.plan.campaign_id,
                    scheduled_trials=len(config.plan.run_ids),
                )
                exit_code = 130
            except KernelError:
                report = CodingEvalCampaignRunReport(
                    reason="runtime_failed",
                    campaign_id=config.plan.campaign_id,
                    scheduled_trials=len(config.plan.run_ids),
                )
            except Exception:
                report = CodingEvalCampaignRunReport(reason="internal_error")
    finally:
        logging.disable(previous_logging)
    print(report.model_dump_json())
    raise SystemExit(exit_code)
