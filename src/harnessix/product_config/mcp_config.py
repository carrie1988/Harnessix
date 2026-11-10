"""用户级MCP配置（ADR 0127）：解析`~/.harnessix/mcp.json`、解析命令并计算信任指纹。

格式沿用通行的`{"mcpServers": {"<id>": {"command", "args", "env"}}}`。
值里的`${NAME}`启动时从用户环境读取，不写入任何持久文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harnessix.execution.contracts import canonical_digest

MCP_CONFIG_FILENAME = "mcp.json"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_SERVER_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})\}")
_SERVER_KEYS = frozenset({"command", "args", "env", "type"})


@dataclass(frozen=True, slots=True)
class McpServerDefinition:
    """配置文件里写下的原样定义；尚未解析命令与环境。"""

    server_id: str
    command: str
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class McpConfigReport:
    """整个文件不可用时只有file_problem；单个Server的问题不影响其他Server。"""

    servers: tuple[McpServerDefinition, ...]
    problems: tuple[tuple[str, str], ...]
    file_problem: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedMcpServer:
    """可以启动的Server：绝对命令、展开后的参数与环境，以及据原样定义计算的指纹。"""

    definition: McpServerDefinition
    command: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    secret_values: tuple[str, ...]
    fingerprint: str


def mcp_config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".harnessix" / MCP_CONFIG_FILENAME


def load_mcp_config(path: Path) -> McpConfigReport:
    """文件不存在时返回空报告：没有MCP配置时产品行为不变。"""

    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return McpConfigReport((), (), "mcp_config_too_large")
        document = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return McpConfigReport((), ())
    except (OSError, ValueError):
        return McpConfigReport((), (), "mcp_config_invalid_json")
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return McpConfigReport((), (), "mcp_config_servers_missing")
    definitions: list[McpServerDefinition] = []
    problems: list[tuple[str, str]] = []
    for server_id, entry in sorted(servers.items()):
        problem = _entry_problem(server_id, entry)
        if problem is not None:
            problems.append((server_id, problem))
            continue
        definitions.append(
            McpServerDefinition(
                server_id=server_id,
                command=entry["command"],
                arguments=tuple(entry.get("args", ())),
                environment=tuple(sorted(entry.get("env", {}).items())),
            )
        )
    return McpConfigReport(tuple(definitions), tuple(problems))


def resolve_mcp_server(
    definition: McpServerDefinition, environ: Mapping[str, str]
) -> tuple[ResolvedMcpServer | None, str | None]:
    """返回（可启动的Server，问题码）；任一引用的环境变量缺失或命令找不到都不启动。"""

    # 只把env里引用的值当作秘密做输出脱敏；命令与参数里的引用多是路径，脱敏会误伤正常输出。
    references: set[str] = set()
    try:
        command_text = _expand(definition.command, environ, set())
        arguments = tuple(_expand(item, environ, set()) for item in definition.arguments)
        environment = {
            name: _expand(value, environ, references) for name, value in definition.environment
        }
    except KeyError as missing:
        return None, f"mcp_env_missing:{missing.args[0]}"
    command = _resolve_command(command_text, environ)
    if command is None:
        return None, "mcp_command_not_found"
    executable = _file_digest(command.resolve())
    if executable is None:
        return None, "mcp_command_unreadable"
    secrets = tuple(sorted({environ[name] for name in references if environ[name]}))
    return (
        ResolvedMcpServer(
            definition=definition,
            command=command,
            arguments=arguments,
            environment=environment,
            secret_values=secrets,
            fingerprint=mcp_definition_fingerprint(definition, command, executable),
        ),
        None,
    )


def mcp_definition_fingerprint(
    definition: McpServerDefinition, command: Path, executable_sha256: str
) -> str:
    """覆盖原样定义、解析后的命令路径与可执行文件字节；环境值只取摘要，不落明文。"""

    return canonical_digest(
        {
            "spec_version": "harnessix.mcp-server-definition/v1",
            "server_id": definition.server_id,
            "command": definition.command,
            "resolved_command": str(command.resolve()),
            "executable_sha256": executable_sha256,
            "arguments": list(definition.arguments),
            "environment": {
                name: hashlib.sha256(value.encode()).hexdigest()
                for name, value in definition.environment
            },
        }
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _entry_problem(server_id: str, entry: object) -> str | None:
    if not _SERVER_ID.fullmatch(server_id):
        return "mcp_server_id_invalid"
    if not isinstance(entry, dict):
        return "mcp_server_entry_invalid"
    if "url" in entry or entry.get("type", "stdio") != "stdio":
        return "mcp_remote_unsupported"
    if set(entry) - _SERVER_KEYS:
        return "mcp_server_field_unsupported"
    command = entry.get("command")
    arguments, environment = entry.get("args", []), entry.get("env", {})
    if not isinstance(command, str) or not command.strip():
        return "mcp_command_missing"
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        return "mcp_args_invalid"
    if not isinstance(environment, dict) or not all(
        isinstance(name, str) and _ENV_NAME.fullmatch(name) and isinstance(value, str)
        for name, value in environment.items()
    ):
        return "mcp_env_invalid"
    return None


def _expand(value: str, environ: Mapping[str, str], references: set[str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        references.add(name)
        return environ[name]

    return _REFERENCE.sub(substitute, value)


def _resolve_command(command: str, environ: Mapping[str, str]) -> Path | None:
    candidate = Path(command).expanduser()
    if candidate.is_absolute():
        found = str(candidate) if os.access(candidate, os.X_OK) and candidate.is_file() else None
    elif os.sep in command or (os.altsep is not None and os.altsep in command):
        # 相对路径会随工作区变化，信任无法绑定到固定程序。
        return None
    else:
        found = shutil.which(command, path=environ.get("PATH", ""))
    # 启动沿用原路径（npx等通常是符号链接）；指纹按链接指向的真实文件计算。
    return Path(found).absolute() if found else None


def _file_digest(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_EXECUTABLE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
