"""汇总每个MCP Server能否启动：配置、命令、环境与信任状态；列表、诊断和会话启动共用。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harnessix.product_config.mcp_config import (
    ResolvedMcpServer,
    load_mcp_config,
    mcp_config_path,
    resolve_mcp_server,
)
from harnessix.product_config.mcp_trust import load_mcp_trust, mcp_trust_path, mcp_trust_state

McpServerState = Literal["trusted", "untrusted", "changed", "unavailable"]

_PROBLEM_TEXT = {
    "mcp_config_too_large": "mcp.json 超过 64 KiB",
    "mcp_config_invalid_json": "mcp.json 不是合法 JSON（或有重复字段）",
    "mcp_config_servers_missing": "mcp.json 缺少 mcpServers 对象",
    "mcp_server_id_invalid": "名称只能用字母开头，含字母、数字、下划线、点和连字符",
    "mcp_server_entry_invalid": "定义必须是对象",
    "mcp_remote_unsupported": "暂不支持远端 MCP（url 或非 stdio 类型）",
    "mcp_server_field_unsupported": "只支持 command、args、env、type 字段",
    "mcp_command_missing": "缺少 command",
    "mcp_args_invalid": "args 必须是字符串数组",
    "mcp_env_invalid": "env 必须是“变量名: 字符串”",
    "mcp_command_not_found": "找不到命令（相对路径不支持，请用命令名或绝对路径）",
    "mcp_command_unreadable": "命令文件无法读取",
}


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    server_id: str
    state: McpServerState
    resolved: ResolvedMcpServer | None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class McpStatusReport:
    servers: tuple[McpServerStatus, ...]
    file_problem: str | None = None


def mcp_server_statuses(environ: Mapping[str, str], *, home: Path | None = None) -> McpStatusReport:
    report = load_mcp_config(mcp_config_path(home))
    if report.file_problem is not None:
        return McpStatusReport((), report.file_problem)
    records = load_mcp_trust(mcp_trust_path(home))
    statuses = [
        McpServerStatus(server_id, "unavailable", None, problem)
        for server_id, problem in report.problems
    ]
    for definition in report.servers:
        resolved, problem = resolve_mcp_server(definition, environ)
        if resolved is None:
            statuses.append(McpServerStatus(definition.server_id, "unavailable", None, problem))
            continue
        state = mcp_trust_state(definition.server_id, resolved.fingerprint, records)
        statuses.append(McpServerStatus(definition.server_id, state, resolved))
    return McpStatusReport(tuple(sorted(statuses, key=lambda item: item.server_id)))


def trusted_mcp_servers(
    environ: Mapping[str, str], *, home: Path | None = None
) -> tuple[ResolvedMcpServer, ...]:
    """会话启动时要连接的Server：配置完整、命令可用、且指纹与信任记录一致。"""

    report = mcp_server_statuses(environ, home=home)
    return tuple(
        status.resolved
        for status in report.servers
        if status.state == "trusted" and status.resolved is not None
    )


def mcp_doctor_lines(environ: Mapping[str, str], *, home: Path | None = None) -> list[str]:
    """`code doctor`的MCP小节；没有配置时不输出。"""

    report = mcp_server_statuses(environ, home=home)
    if report.file_problem is not None:
        return [f"- MCP：{mcp_problem_text(report.file_problem)}"]
    lines = []
    for status in report.servers:
        if status.state == "trusted":
            lines.append(f"- MCP {status.server_id}：已信任，会话启动时运行")
        elif status.state == "unavailable":
            lines.append(
                f"- MCP {status.server_id}：不可用（{mcp_problem_text(status.problem or '')}）"
            )
        else:
            hint = "定义已变化，" if status.state == "changed" else ""
            lines.append(
                f"- MCP {status.server_id}：{hint}未信任，不会启动；"
                f"运行 harnessix mcp trust {status.server_id}"
            )
    return lines


def mcp_problem_text(code: str) -> str:
    if code.startswith("mcp_env_missing:"):
        return f"环境变量 {code.split(':', 1)[1]} 未设置"
    return _PROBLEM_TEXT.get(code, code)
