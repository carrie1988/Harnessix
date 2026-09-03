from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
from collections.abc import Sequence
from typing import NoReturn

from harnessix.models._json import strict_json
from harnessix.smoke.contracts import SmokeConfig, SmokeReport
from harnessix.smoke.runner import run_smoke


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse 默认会回显错误参数，其中可能含凭据。
        self.exit(2, "model-smoke 参数无效；使用 --help 查看格式。\n")


def _read_config(path: str) -> SmokeConfig:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("配置必须是普通文件")
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ValueError("配置超过上限")
    return SmokeConfig.model_validate(strict_json(raw.decode("utf-8")))


def main(argv: Sequence[str]) -> None:
    parser = _SafeParser(
        prog="harnessix model-smoke",
        description="固定场景模型验收；默认不读取凭据或调用 API",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, help="UTF-8 JSON 配置文件（仅凭据环境引用）")
    parser.add_argument("--allow-network", action="store_true", help="允许执行固定场景模型请求")
    arguments = parser.parse_args(argv)
    # 禁用时连配置文件都不读取，避免帮助/预检触发任何 Provider 行为。
    if not arguments.allow_network:
        print(
            SmokeReport(
                reason="network_not_enabled", attempts_started=0, tool_calls=0
            ).model_dump_json()
        )
        raise SystemExit(2)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    exit_code = 1
    try:
        try:
            config = _read_config(arguments.config)
        except (OSError, ValueError, RecursionError):
            report = SmokeReport(reason="configuration_invalid")
            exit_code = 2
        else:
            report = asyncio.run(run_smoke(config, allow_network=True))
            exit_code = 0 if report.reason == "passed" else 1
    except KeyboardInterrupt:
        report = SmokeReport(reason="cancelled")
        exit_code = 130
    except Exception:
        report = SmokeReport(reason="internal_error")
    finally:
        logging.disable(previous_logging)
    print(report.model_dump_json())
    raise SystemExit(exit_code)
