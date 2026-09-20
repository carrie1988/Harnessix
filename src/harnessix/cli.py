"""解析Harnessix Code顶层命令并分派产品、Eval和配置子命令。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from harnessix.licensing import render_license_notice


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harnessix Code")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("model-smoke", help="运行显式启用的固定场景模型验收")
    subcommands.add_parser(
        "coding-eval-campaign", help="运行显式启用的固定历史任务真实模型Campaign"
    )
    subcommands.add_parser(
        "coding-eval-suite", help="运行显式启用的固定Task Pack真实Provider Suite"
    )
    subcommands.add_parser("agent", help="通过stdio App Server运行薄Agent CLI")
    subcommands.add_parser("code", help="启动全屏Coding Agent终端产品")
    subcommands.add_parser("agent-server", help="按产品配置运行stdio App Server")
    subcommands.add_parser("config", help="诊断或迁移产品配置")
    subcommands.add_parser("license", help="显示社区许可证、源代码和商业许可信息")
    return parser


def _delegate_special_command(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "model-smoke":
        from harnessix.smoke.cli import main as smoke_main

        smoke_main(args[1:])
        return True
    if args[0] == "coding-eval-campaign":
        from harnessix.evals.campaign_cli import main as campaign_main

        campaign_main(args[1:])
        return True
    if args[0] == "coding-eval-suite":
        from harnessix.evals.provider_suite_cli import main as provider_suite_main

        provider_suite_main(args[1:])
        return True
    if args[0] == "agent":
        from harnessix.agent_cli import main as agent_main

        agent_main(args[1:])
        return True
    if args[0] == "agent-server":
        from harnessix.product_config.cli import agent_server_main

        agent_server_main(args[1:])
        return True
    if args[0] == "code":
        from harnessix.product_ui.cli import code_main

        code_main(args[1:])
        return True
    if args[0] == "config":
        from harnessix.product_config.cli import config_main

        config_main(args[1:])
        return True
    return False


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if _delegate_special_command(args):
        return
    arguments = _parser().parse_args(args)
    if arguments.command == "license":
        print(render_license_notice())
        return
