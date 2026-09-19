"""从受控路径装配产品stdio运行时，并在平台或依赖不满足时于开放协议前失败关闭。"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.action_codec import (
    load_product_action_config,
    product_action_config_snapshot,
)
from harnessix.product_config.action_contracts import (
    ProductActionConfigSnapshot,
    ProductActionConfigV1,
)
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, ProfileSelection
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from harnessix.product_config.product_contracts import ProductPreflightReport
from harnessix.product_config.runtime import (
    build_provider_bundle,
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.secrets.provider import SecretProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


@dataclass(frozen=True, slots=True)
class _ProductRuntimeStartup:
    """保存预检完成后进入托管生命周期所需的固定启动事实。"""

    config: ProductConfigSnapshot
    profile: ProfileSelection
    actions: ProductActionConfigSnapshot
    workspace_root: Path
    state_root: Path
    git_path: Path | None
    secrets: SecretProvider


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
    action_config_path: str | Path | None,
    profile_id: str | None,
    workspace: str | Path,
    state_directory: str | Path,
    git_executable: str | Path | None,
) -> ProductPreflightRequest:
    return ProductPreflightRequest(
        mode="startup",
        config_path=Path(config_path).absolute(),
        action_config_path=(
            Path(action_config_path).absolute() if action_config_path is not None else None
        ),
        profile_id=profile_id,
        workspace=Path(workspace).absolute(),
        state_directory=Path(state_directory).absolute(),
        git_executable=Path(git_executable).absolute() if git_executable is not None else None,
        require_tui=False,
    )


async def _require_startup_preflight(request: ProductPreflightRequest) -> ProductPreflightReport:
    preflight = await asyncio.to_thread(run_product_preflight, request)
    if preflight.ready:
        return preflight
    blocker = next(
        item
        for item in preflight.checks
        if item.requirement == "required" and item.status != "passed"
    )
    raise KernelError(blocker.code, "产品启动预检未通过")


async def _validated_workspace(
    *,
    config_path: str | Path,
    action_config_path: str | Path | None,
    workspace: str | Path,
) -> Path:
    """解析固定Workspace，并拒绝把产品配置置于其内部。"""

    workspace_root = await asyncio.to_thread(_workspace_root, workspace)
    config_file = await asyncio.to_thread(_configuration_file, config_path)
    if config_file.is_relative_to(workspace_root):
        raise KernelError("product_config_overlap", "产品配置文件不能位于Workspace内")
    if action_config_path is not None:
        try:
            action_file = await asyncio.to_thread(
                lambda: Path(action_config_path).resolve(strict=True)
            )
        except (OSError, RuntimeError):
            raise KernelError(
                "product_action_config_changed", "Product Action配置路径在启动期间发生变化"
            ) from None
        if action_file.is_relative_to(workspace_root):
            raise KernelError(
                "product_action_config_overlap", "Product Action配置文件不能位于Workspace内"
            )
    return workspace_root


async def _validated_runtime_paths(
    *,
    workspace_root: Path,
    state_directory: str | Path,
    git_executable: str | Path | None,
) -> tuple[Path, Path | None]:
    """解析产品根并在创建私有状态前后拒绝Workspace重叠。"""

    state_candidate = await asyncio.to_thread(_absolute_path, state_directory)
    if state_candidate.is_relative_to(workspace_root) or workspace_root.is_relative_to(
        state_candidate
    ):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
    state_root = await asyncio.to_thread(_private_root, state_directory)
    if state_root.is_relative_to(workspace_root) or workspace_root.is_relative_to(state_root):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
    git_path = (
        await asyncio.to_thread(_git_path, git_executable) if git_executable is not None else None
    )
    return state_root, git_path


async def _serve_product_stdio(
    startup: _ProductRuntimeStartup,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    expected_active_sha256: str | None,
    expected_active_profile: str | None,
    expected_active_action_sha256: str | None,
) -> None:
    """在单一产品进程内持有Provider、Tool、Action和Agent完整生命周期。"""

    with SQLiteProductRuntimeConfigStore(startup.state_root / "product-config.db") as config_store:
        config_store.save_snapshot(startup.config)
        config_store.save_action_snapshot(startup.actions)
        previous_action_sha256 = config_store.active_action()
        recovery_action = (
            startup.actions.config
            if previous_action_sha256 is None
            else config_store.load_action_snapshot(previous_action_sha256).config
        )
        bundle = await build_provider_bundle(
            startup.config,
            startup.profile,
            startup.secrets,
            audit=config_store,
        )
        async with bundle:
            # Provider Bundle先进入托管生命周期，确保后续初始化失败时仍会关闭全部SDK Client。
            sessions = SQLiteSessionStore(startup.state_root / "sessions.db")
            await sessions.initialize()
            requests = SQLiteProtocolRequestStore(sessions.path)
            artifacts = SQLiteArtifactStore(sessions)
            async with CodingToolRuntime(
                startup.workspace_root,
                artifacts=artifacts,
                git_executable=startup.git_path,
            ) as tools:
                async with open_default_product_action_runtime(
                    startup.state_root,
                    startup.workspace_root,
                    artifacts,
                    startup.secrets,
                    startup.actions.config,
                    artifact_workspace_scope=tools.workspace_scope,
                    recovery_config=recovery_action,
                ) as action_owner:
                    config_store.save_action_recovery_report(action_owner.recovery)
                    async with AgentRuntime(
                        sessions,
                        bundle,
                        scoped_tools=tools,
                        artifacts=artifacts,
                        trusted_actions=action_owner.gateway,
                    ) as runtime:
                        # 全部组件就绪后才原子发布Product与Action活动指针并开放stdio。
                        config_store.activate_runtime(
                            startup.config,
                            startup.profile.selected_profile,
                            startup.actions,
                            expected_active_sha256=expected_active_sha256,
                            expected_active_profile=expected_active_profile,
                            expected_active_action_sha256=expected_active_action_sha256,
                        )
                        service = AgentApplicationService(
                            runtime,
                            sessions,
                            requests,
                            ScopedProtocolArtifactReader(sessions, artifacts, tools),
                            workspace=startup.workspace_root,
                        )
                        await run_stdio(AgentProtocolServer(service), input_stream, output_stream)


async def run_product_stdio(
    *,
    config_path: str | Path,
    action_config_path: str | Path | None = None,
    profile_id: str | None,
    workspace: str | Path,
    state_directory: str | Path,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    expected_active_sha256: str | None = None,
    expected_active_profile: str | None = None,
    expected_active_action_sha256: str | None = None,
    git_executable: str | Path | None = None,
    action_config: ProductActionConfigV1 | None = None,
) -> None:
    if action_config_path is not None and action_config is not None:
        raise KernelError("product_action_config_conflict", "不能同时传入Action配置文件和内存配置")
    request = _preflight_request(
        config_path=config_path,
        action_config_path=action_config_path,
        profile_id=profile_id,
        workspace=workspace,
        state_directory=state_directory,
        git_executable=git_executable,
    )
    preflight = await _require_startup_preflight(request)

    loaded = load_product_config(config_path)
    if not isinstance(loaded, ProductConfigSnapshot):
        raise KernelError("product_config_migration_required", "产品配置必须先迁移到v2")
    if loaded.config_sha256 != preflight.config_sha256:
        raise KernelError("product_config_changed", "产品配置在预检后发生变化")
    selection = select_profile(loaded, profile_id)
    workspace_root = await _validated_workspace(
        config_path=config_path,
        action_config_path=action_config_path,
        workspace=workspace,
    )
    secrets = environment_secret_provider(loaded.config)
    action_snapshot = (
        product_action_config_snapshot(action_config)
        if action_config is not None
        else load_product_action_config(action_config_path)
    )
    if action_config is None and action_snapshot.config_sha256 != preflight.action_config_sha256:
        raise KernelError("product_action_config_changed", "Product Action配置在预检后发生变化")
    report = diagnose_configuration(loaded, selection, secrets)
    if not report.ready:
        raise KernelError("product_config_diagnostic_failed", "产品配置离线诊断未通过")
    state_root, git_path = await _validated_runtime_paths(
        workspace_root=workspace_root,
        state_directory=state_directory,
        git_executable=git_executable,
    )
    startup = _ProductRuntimeStartup(
        config=loaded,
        profile=selection,
        actions=action_snapshot,
        workspace_root=workspace_root,
        state_root=state_root,
        git_path=git_path,
        secrets=secrets,
    )
    await _serve_product_stdio(
        startup,
        input_stream=input_stream,
        output_stream=output_stream,
        expected_active_sha256=expected_active_sha256,
        expected_active_profile=expected_active_profile,
        expected_active_action_sha256=expected_active_action_sha256,
    )
