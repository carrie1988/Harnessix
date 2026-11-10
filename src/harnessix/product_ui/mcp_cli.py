"""`harnessix mcp list|trust|untrust`：查看并逐个信任本机MCP Server（ADR 0126）。"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from harnessix.product_config.mcp_status import (
    McpServerStatus,
    mcp_problem_text,
    mcp_server_statuses,
)
from harnessix.product_config.mcp_trust import (
    mcp_trust_path,
    trust_mcp_server,
    untrust_mcp_server,
)

_STATE_TEXT = {
    "trusted": "已信任",
    "untrusted": "未信任",
    "changed": "定义已变化，需重新信任",
    "unavailable": "不可用",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix mcp",
        description="查看并信任 ~/.harnessix/mcp.json 中的 MCP Server",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出 MCP Server 及其状态")
    trust = commands.add_parser("trust", help="信任一个 MCP Server，此后会话启动时会运行它")
    trust.add_argument("server_id")
    untrust = commands.add_parser("untrust", help="撤销对一个 MCP Server 的信任")
    untrust.add_argument("server_id")
    return parser


def _describe(status: McpServerStatus) -> str:
    line = f"{status.server_id}  [{_STATE_TEXT[status.state]}]"
    if status.resolved is not None:
        resolved = status.resolved
        line += f"  {resolved.command} {' '.join(resolved.definition.arguments)}".rstrip()
    if status.problem is not None:
        line += f"  {mcp_problem_text(status.problem)}"
    return line


def _list() -> int:
    report = mcp_server_statuses(os.environ)
    if report.file_problem is not None:
        print(mcp_problem_text(report.file_problem), file=sys.stderr)
        return 2
    if not report.servers:
        print("没有配置 MCP Server（~/.harnessix/mcp.json）。")
        return 0
    print("\n".join(_describe(status) for status in report.servers))
    return 0


def _trust(server_id: str) -> int:
    report = mcp_server_statuses(os.environ)
    status = next((item for item in report.servers if item.server_id == server_id), None)
    if status is None or status.resolved is None:
        reason = report.file_problem or (status.problem if status else "mcp_server_not_configured")
        print(f"无法信任 {server_id}：{mcp_problem_text(reason or '')}", file=sys.stderr)
        return 2
    resolved = status.resolved
    names = ", ".join(name for name, _ in resolved.definition.environment) or "无"
    print(
        f"将信任 {server_id}：\n"
        f"  命令：{resolved.command}\n"
        f"  参数：{' '.join(resolved.definition.arguments) or '无'}\n"
        f"  环境变量：{names}\n"
        "它会以你的权限在本机运行，能读写你能访问的文件、访问网络。"
        "定义或命令文件变化后需要重新信任；npx、uvx 下载的包版本请在参数里固定。"
    )
    trust_mcp_server(mcp_trust_path(), server_id, resolved.fingerprint)
    print("已信任。")
    return 0


def mcp_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    if arguments.command == "list":
        raise SystemExit(_list())
    if arguments.command == "trust":
        raise SystemExit(_trust(arguments.server_id))
    removed = untrust_mcp_server(mcp_trust_path(), arguments.server_id)
    print("已撤销信任。" if removed else f"{arguments.server_id} 本来就未被信任。")
