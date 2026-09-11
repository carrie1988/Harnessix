"""从受控路径装配产品stdio运行时，并在平台或依赖不满足时于开放协议前失败关闭。"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import BinaryIO

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot
from harnessix.product_config.runtime import (
    build_provider_bundle,
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)
from harnessix.product_config.store import SQLiteProductConfigStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _private_root(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    try:
        if candidate.exists() or _is_link_or_junction(candidate):
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode) or _is_link_or_junction(candidate):
                raise OSError
        else:
            candidate.mkdir(parents=True, mode=0o700)
        root = candidate.resolve(strict=True)
        if os.name == "posix":
            info = root.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError
            root.chmod(0o700)
        return root
    except (OSError, RuntimeError):
        raise KernelError("product_state_invalid", "产品状态目录权限或身份无效") from None


def _workspace_root(path: str | Path) -> Path:
    try:
        root = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("product_workspace_invalid", "产品Workspace不可用") from None
    if not root.is_dir():
        raise KernelError("product_workspace_invalid", "产品Workspace不是目录")
    return root


def _require_coding_tool_platform() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise KernelError(
            "product_tools_platform_unsupported",
            "内置只读Coding Tool Runtime当前不支持该宿主平台",
        )


def _configuration_file(path: str | Path) -> Path:
    try:
        return Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("product_config_changed", "产品配置路径在启动期间发生变化") from None


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


def _git_path(path: str | Path) -> Path:
    try:
        executable = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("product_git_invalid", "Git可执行文件不可用") from None
    if not executable.is_file():
        raise KernelError("product_git_invalid", "Git可执行文件不是普通文件")
    return executable


async def run_product_stdio(
    *,
    config_path: str | Path,
    profile_id: str | None,
    workspace: str | Path,
    state_directory: str | Path,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    expected_active_sha256: str | None = None,
    expected_active_profile: str | None = None,
    git_executable: str | Path | None = None,
) -> None:
    loaded = load_product_config(config_path)
    if not isinstance(loaded, ProductConfigSnapshot):
        raise KernelError("product_config_migration_required", "产品配置必须先迁移到v2")
    selection = select_profile(loaded, profile_id)
    workspace_root = await asyncio.to_thread(_workspace_root, workspace)
    config_file = await asyncio.to_thread(_configuration_file, config_path)
    if config_file.is_relative_to(workspace_root):
        raise KernelError("product_config_overlap", "产品配置文件不能位于Workspace内")
    secrets = environment_secret_provider(loaded.config)
    report = diagnose_configuration(loaded, selection, secrets)
    if not report.ready:
        raise KernelError("product_config_diagnostic_failed", "产品配置离线诊断未通过")
    state_candidate = await asyncio.to_thread(_absolute_path, state_directory)
    if state_candidate.is_relative_to(workspace_root) or workspace_root.is_relative_to(
        state_candidate
    ):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
    _require_coding_tool_platform()
    state_root = await asyncio.to_thread(_private_root, state_directory)
    if state_root.is_relative_to(workspace_root) or workspace_root.is_relative_to(state_root):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
    git_path = None
    if git_executable is not None:
        git_path = await asyncio.to_thread(_git_path, git_executable)

    with SQLiteProductConfigStore(state_root / "product-config.db") as config_store:
        config_store.save_snapshot(loaded)
        bundle = await build_provider_bundle(
            loaded,
            selection,
            secrets,
            audit=config_store,
        )
        async with bundle:
            # Provider Bundle先进入托管生命周期，确保后续Session或Runtime初始化失败时
            # 仍会关闭全部SDK Client。
            sessions = SQLiteSessionStore(state_root / "sessions.db")
            await sessions.initialize()
            requests = SQLiteProtocolRequestStore(sessions.path)
            async with (
                CodingToolRuntime(workspace_root, git_executable=git_path) as tools,
                AgentRuntime(sessions, bundle, scoped_tools=tools) as runtime,
            ):
                # 全部组件成功进入生命周期后才以CAS发布活动指针；冲突会逆序关闭
                # Runtime、Tool和Provider，且不会开放stdio或创建Thread。
                config_store.activate(
                    loaded,
                    selection.selected_profile,
                    expected_active_sha256=expected_active_sha256,
                    expected_active_profile=expected_active_profile,
                )
                service = AgentApplicationService(
                    runtime,
                    sessions,
                    requests,
                    workspace=workspace_root,
                )
                await run_stdio(
                    AgentProtocolServer(service),
                    input_stream,
                    output_stream,
                )
