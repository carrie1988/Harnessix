"""从受控路径装配产品stdio运行时，并在平台或依赖不满足时于开放协议前失败关闭。"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import BinaryIO

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.action_runtime import open_default_workspace_patch_runtime
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
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


def _preflight_request(
    *,
    config_path: str | Path,
    profile_id: str | None,
    workspace: str | Path,
    state_directory: str | Path,
    git_executable: str | Path | None,
) -> ProductPreflightRequest:
    return ProductPreflightRequest(
        mode="startup",
        config_path=Path(config_path).absolute(),
        profile_id=profile_id,
        workspace=Path(workspace).absolute(),
        state_directory=Path(state_directory).absolute(),
        git_executable=Path(git_executable).absolute() if git_executable is not None else None,
        require_tui=False,
    )


async def _require_startup_preflight(request: ProductPreflightRequest) -> None:
    preflight = await asyncio.to_thread(run_product_preflight, request)
    if preflight.ready:
        return
    blocker = next(
        item
        for item in preflight.checks
        if item.requirement == "required" and item.status != "passed"
    )
    raise KernelError(blocker.code, "产品启动预检未通过")


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
    request = _preflight_request(
        config_path=config_path,
        profile_id=profile_id,
        workspace=workspace,
        state_directory=state_directory,
        git_executable=git_executable,
    )
    await _require_startup_preflight(request)

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
            artifacts = SQLiteArtifactStore(sessions)
            async with CodingToolRuntime(
                workspace_root,
                artifacts=artifacts,
                git_executable=git_path,
            ) as tools:
                with open_default_workspace_patch_runtime(
                    state_root,
                    workspace_root,
                    artifacts,
                    artifact_workspace_scope=tools.workspace_scope,
                ) as composition:
                    async with AgentRuntime(
                        sessions,
                        bundle,
                        scoped_tools=tools,
                        artifacts=artifacts,
                        trusted_actions=composition.gateway,
                    ) as runtime:
                        # 全部组件成功进入生命周期后才以CAS发布活动指针；冲突会逆序关闭
                        # Runtime、Tool、Action Store和Provider，且不会开放stdio或创建Thread。
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
                            ScopedProtocolArtifactReader(sessions, artifacts, tools),
                            workspace=workspace_root,
                        )
                        await run_stdio(
                            AgentProtocolServer(service),
                            input_stream,
                            output_stream,
                        )
