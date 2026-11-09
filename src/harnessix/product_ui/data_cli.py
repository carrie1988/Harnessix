"""用户数据管理：列出、导出和删除本机会话数据，生成不含正文的诊断信息。

删除必须输入目录指纹确认，并持有状态锁，运行中的工作区会被拒绝。
卸载产品不会调用这里的逻辑，用户数据只有在这里才会被删除。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
from collections.abc import Iterable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.product_config.preflight import run_product_preflight
from harnessix.product_config.state_owner import product_state_owner
from harnessix.product_ui.cli import (
    _default_config,
    _default_state_directory,
    _preflight_request,
    _server_command,
    _workspace,
)
from harnessix.product_ui.contracts import workspace_identity_fingerprint
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.git_detection import detected_git_executable
from harnessix.protocol.contracts import ThreadView
from harnessix.sdk import AgentClient, AgentSDKError, SubprocessAgentTransport

_REPLAY_PAGE = 256


def workspaces_root() -> Path:
    return Path.home() / ".harnessix" / "workspaces"


def _directory_size(path: Path) -> int:
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(Path(directory) / name).st_size
            except OSError:
                continue
    return total


def list_workspaces(root: Path) -> list[dict[str, object]]:
    """每个工作区状态目录的指纹、大小和最近修改时间，不读取会话内容。"""

    if not root.is_dir():
        return []
    return [
        {
            "fingerprint": entry.name,
            "size_bytes": _directory_size(entry),
            "modified": int(entry.stat().st_mtime),
        }
        for entry in sorted(root.iterdir())
        if entry.is_dir() and not entry.is_symlink()
    ]


def delete_workspace_state(root: Path, fingerprint: str, *, confirm: str) -> None:
    """确认指纹一致且工作区没有运行中的宿主时，删除该工作区的全部会话数据。"""

    if confirm != fingerprint:
        raise ProductUIError("data_delete_unconfirmed", f"确认删除请加上 --confirm {fingerprint}")
    target = root / fingerprint
    if Path(fingerprint).name != fingerprint or target.is_symlink() or not target.is_dir():
        raise ProductUIError("data_workspace_missing", "没有找到该工作区的数据")
    runtime = target / "runtime"
    try:
        if not runtime.is_dir():
            shutil.rmtree(target)
            return
        with product_state_owner(runtime):
            shutil.rmtree(target)
    except KernelError as error:
        if error.code == "product_state_busy":
            raise ProductUIError("data_workspace_busy", "该工作区正在运行，请先退出") from None
        raise ProductUIError("data_delete_failed", "无法锁定该工作区的状态") from None
    except OSError:
        raise ProductUIError("data_delete_failed", "删除数据时发生文件错误") from None


def thread_diagnostics(thread: ThreadView) -> dict[str, object]:
    """只保留状态、错误码和用量；不含路径、提示词、模型输出或工具内容。"""

    turn = thread.latest_turn
    return {
        "thread_id": str(thread.thread_id),
        "turn_count": thread.turn_count,
        "archived": thread.archive is not None,
        "latest_turn": None
        if turn is None
        else {
            "status": turn.status,
            "error_code": None if turn.error is None else turn.error.code,
            "model_steps": turn.model_steps,
            "input_tokens": turn.usage.input_tokens,
            "output_tokens": turn.usage.output_tokens,
        },
    }


async def _all_threads(client: AgentClient) -> list[ThreadView]:
    threads: list[ThreadView] = []
    for archived in (False, True):
        cursor: str | None = None
        while True:
            page = await client.list_threads(cursor=cursor, limit=200, archived=archived)
            threads.extend(page.threads)
            cursor = page.next_cursor
            if cursor is None:
                break
    return threads


async def _thread_events(client: AgentClient, thread: ThreadView) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = await client.replay_events(thread.thread_id, after_cursor=cursor, limit=_REPLAY_PAGE)
        events.extend(event.model_dump(mode="json", by_alias=True) for event in page.events)
        if not page.has_more or page.scanned_through <= cursor:
            return events
        cursor = page.scanned_through


def _write_private_json(path: Path, value: object) -> None:
    """只创建新文件，权限仅当前用户可读写，避免覆盖已有文件。"""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _client(workspace: Path, arguments: argparse.Namespace) -> AgentClient:
    # 必须与产品运行时使用同样的启动参数，否则状态目录会因配置变化拒绝打开。
    config = Path(arguments.config).absolute() if arguments.config else _default_config()
    command = _server_command(
        config=config,
        action_config=None,
        profile=arguments.profile,
        workspace=workspace,
        runtime_state=_default_state_directory(workspace) / "runtime",
        git_executable=detected_git_executable(None, workspace),
    )
    return AgentClient(SubprocessAgentTransport(command), client_name="harnessix-data")


async def _read_threads(
    workspace: Path, arguments: argparse.Namespace, *, with_events: bool
) -> list[dict[str, object]]:
    client = _client(workspace, arguments)
    try:
        await client.initialize()
        threads = await _all_threads(client)
        if not with_events:
            return [thread_diagnostics(item) for item in threads]
        return [
            {
                "thread": item.model_dump(mode="json", by_alias=True),
                "events": await _thread_events(client, item),
            }
            for item in threads
        ]
    finally:
        await client.close()


def _doctor(arguments: argparse.Namespace) -> dict[str, object]:
    request = argparse.Namespace(
        workspace=arguments.workspace,
        config=arguments.config,
        action_config=None,
        profile=arguments.profile,
        state_directory=None,
        git_executable=detected_git_executable(None, Path(arguments.workspace).absolute()),
    )
    report = run_product_preflight(_preflight_request(request, mode="doctor", require_tui=False))
    return report.model_dump(mode="json")


def _installed_version() -> str:
    try:
        return version("harnessix")
    except PackageNotFoundError:
        return "unknown"


def _diagnostics(arguments: argparse.Namespace) -> dict[str, object]:
    workspace = _workspace(arguments.workspace)
    result: dict[str, object] = {
        "spec_version": "harnessix.diagnostics/v1",
        "harnessix_version": _installed_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "doctor": _doctor(arguments),
    }
    try:
        result["threads"] = asyncio.run(_read_threads(workspace, arguments, with_events=False))
    except AgentSDKError as error:
        result["threads_error"] = error.code
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnessix data", description="管理本机的会话数据")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("list", help="列出各工作区的数据目录")
    delete = commands.add_parser("delete", help="删除一个工作区的全部会话数据")
    target = delete.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace")
    target.add_argument("--fingerprint")
    delete.add_argument("--confirm", required=True, help="再次输入目录指纹以确认删除")
    for name, text in (("export", "导出完整会话"), ("diagnostics", "生成不含正文的诊断信息")):
        command = commands.add_parser(name, help=text)
        command.add_argument("workspace", nargs="?", default=".")
        command.add_argument("--output", required=True)
        command.add_argument("--config")
        command.add_argument("--profile")
    return parser


def _run(arguments: argparse.Namespace) -> Iterable[object]:
    root = workspaces_root()
    if arguments.operation == "list":
        return list_workspaces(root)
    if arguments.operation == "delete":
        fingerprint = arguments.fingerprint or workspace_identity_fingerprint(
            str(Path(arguments.workspace).resolve())
        )
        delete_workspace_state(root, fingerprint, confirm=arguments.confirm)
        return [{"deleted": fingerprint}]
    output = Path(arguments.output).absolute()
    if arguments.operation == "export":
        workspace = _workspace(arguments.workspace)
        threads = asyncio.run(_read_threads(workspace, arguments, with_events=True))
        _write_private_json(
            output, {"spec_version": "harnessix.data-export/v1", "threads": threads}
        )
    else:
        _write_private_json(output, _diagnostics(arguments))
    return [{"written": str(output)}]


def data_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
        result = _run(arguments)
    except ProductUIError as error:
        print(json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False))
        raise SystemExit(2) from None
    except FileExistsError:
        print(json.dumps({"code": "data_output_exists", "message": "输出文件已存在"}))
        raise SystemExit(2) from None
    except AgentSDKError as error:
        print(
            json.dumps(
                {"code": error.code, "message": "无法读取会话，请先运行 harnessix code doctor"},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stdout)
