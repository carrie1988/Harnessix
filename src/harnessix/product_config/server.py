"""从受控路径装配产品stdio运行时，并在平台或依赖不满足时于开放协议前失败关闭。"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

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
    ProductNativeProcessProfile,
)
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from harnessix.product_config.contracts import (
    ModelProfile,
    ProductConfigSnapshot,
    ProfileSelection,
    ProviderDefinition,
)
from harnessix.product_config.native_process import native_tool_name
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from harnessix.product_config.product_contracts import ProductPreflightReport
from harnessix.product_config.runtime import (
    build_provider_bundle,
    diagnose_configuration,
    environment_secret_provider,
    provider_secret_references,
    select_profile,
)
from harnessix.product_config.session_key import open_product_session_binding
from harnessix.product_config.skill_composition import (
    product_skill_sources,
    skill_context_fragments,
)
from harnessix.product_config.state_owner import ProductStateOwner, product_state_owner
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sandbox.contracts import DEFAULT_CONTAINER_WORKSPACE
from harnessix.secrets.provider import SecretProvider
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.maintenance_io import MaintenanceIOControl, run_maintenance_io
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS
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
    root_owner: ProductStateOwner
    completion_guard: bool = False
    continue_after_unknown_usage: bool = False


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _private_root(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    try:
        if os.name == "nt":
            from harnessix.workspace.windows_private_directory import private_state_directory

            private_state_directory(candidate, parents=True, exist_ok=True)
            return candidate.resolve(strict=True)
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
    except (OSError, RuntimeError, KernelError):
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
    if os.name == "nt":
        from harnessix.workspace.git_windows_binding import validate_windows_git_executable

        return validate_windows_git_executable(Path(path))
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
    zero_config_actions: bool = False,
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
        git_executable=Path(git_executable) if git_executable is not None else None,
        require_tui=False,
        zero_config_actions=zero_config_actions and action_config_path is None,
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
    state_root = await run_maintenance_io(
        lambda control: _prepare_state_root(state_directory, control), budget_seconds=5.0
    )
    if state_root.is_relative_to(workspace_root) or workspace_root.is_relative_to(state_root):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
    git_path = (
        await asyncio.to_thread(_git_path, git_executable) if git_executable is not None else None
    )
    return state_root, git_path


def _prepare_state_root(path: str | Path, control: MaintenanceIOControl) -> Path:
    """只派发一次目录准备；父取消先结算原写入线程，随后才允许产品Owner释放。"""
    control.checkpoint()
    root = _private_root(path)
    control.checkpoint()
    return root


def _check_state_overlap(state_directory: str | Path, workspace_root: Path) -> None:
    """只读核对根地址及已存在的父目录别名，不向Workspace创建外部锁锚点。"""
    try:
        candidate = Path(state_directory).absolute().resolve(strict=False)
    except (OSError, RuntimeError):
        raise KernelError("product_state_invalid", "产品状态目录地址无效") from None
    if candidate.is_relative_to(workspace_root) or workspace_root.is_relative_to(candidate):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")


def _container_workspace_paths(actions: ProductActionConfigV1) -> tuple[str, ...]:
    """命令容器内工作区挂载点：读工具据此换算模型误用的容器绝对路径。"""
    return tuple(
        sorted(
            {
                profile.workspace_path or DEFAULT_CONTAINER_WORKSPACE
                for profile in actions.process_profiles
            }
        )
    )


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

    with (
        SQLiteProductRuntimeConfigStore(startup.state_root / "product-config.db") as config_store,
        SecretPublicationScope(
            provider_secret_references(startup.config, startup.profile), startup.secrets
        ) as public_scope,
    ):
        config_store.save_snapshot(startup.config)
        config_store.save_action_snapshot(startup.actions)
        previous_action_sha256 = config_store.active_action()
        recovery_action = (
            startup.actions.config
            if previous_action_sha256 is None
            else config_store.load_action_snapshot(previous_action_sha256).config
        )
        async with open_product_session_binding(startup.state_root, public_scope) as binding:
            # 原事实和稳定Key先验真；未知历史、丢失密钥不得构造Provider或开放协议。
            sessions = SQLiteSessionStore(startup.state_root / "sessions.db", publication=binding)
            await sessions.initialize()
            bundle = await build_provider_bundle(
                startup.config, startup.profile, public_scope, audit=config_store
            )
            async with bundle:
                requests = SQLiteProtocolRequestStore(sessions.path)
                artifacts = SQLiteArtifactStore(sessions, public_output_protection=public_scope)
                async with CodingToolRuntime(
                    startup.workspace_root,
                    artifacts=artifacts,
                    git_executable=startup.git_path,
                    git_state_directory=startup.state_root,
                    git_output_redaction=public_scope,
                    enable_regex_search=True,
                    read_timeout_seconds=(
                        startup.config.config.workspace_read_timeout_seconds or READ_TIMEOUT_SECONDS
                    ),
                    container_workspace_paths=_container_workspace_paths(startup.actions.config),
                ) as tools:
                    async with open_default_product_action_runtime(
                        startup.state_root,
                        startup.workspace_root,
                        artifacts,
                        startup.secrets,
                        startup.actions.config,
                        artifact_workspace_scope=tools.workspace_scope,
                        recovery_config=recovery_action,
                        output_redaction=public_scope,
                        root_owner=startup.root_owner,
                        skill_sources=product_skill_sources(startup.workspace_root),
                    ) as action_owner:
                        config_store.save_action_recovery_scan(action_owner.recovery_scan)
                        config_store.save_action_recovery_report(action_owner.recovery)
                        if os.name == "nt":
                            from harnessix.processes.git_read_windows import (
                                reconcile_windows_git_reads,
                            )

                            await reconcile_windows_git_reads(startup.state_root)
                        # 只有实际探测通过的通用执行能力才切换指令/工具视图；配置本身不授予权限。
                        verified = {
                            capability.capability_id
                            for capability in action_owner.report.capabilities
                            if capability.status == "verified"
                        }
                        generic_execution = any(
                            tool.startswith(("exec_command.", "host_command.")) for tool in verified
                        )
                        # 原生命令按后端选用如实描述其执行语义的指令；容器命令保持通用指令。
                        native_instructions = _native_instructions(
                            verified, startup.actions.config.native_process_profiles
                        )
                        chain = _active_profiles(startup)
                        context = build_product_agent_context(
                            tools.workspace_root,
                            max_output_tokens=max(profile.max_output_tokens for profile in chain),
                            context_window_tokens=_chain_context_window(chain),
                            context_compaction=startup.config.config.compaction,
                            generic_execution=generic_execution,
                            native_execution=native_instructions == "native",
                            external_boundary_execution=native_instructions == "external_boundary",
                            enable_planning=True,
                            enable_regex_search=True,
                            extra_fragments=skill_context_fragments(
                                action_owner.skills, tools_visible=generic_execution
                            ),
                        )
                        async with AgentRuntime(
                            sessions,
                            bundle,
                            scoped_tools=tools,
                            artifacts=artifacts,
                            public_output_protection=public_scope,
                            trusted_actions=action_owner.agent_gateway,
                            approval_responder=action_owner.approval_responder,
                            async_context=context.context,
                            compaction=context.compaction,
                            summary_provider=bundle,
                            model_tool_view=None if generic_execution else CodingWorkflowToolView(),
                            enable_planning=True,
                            normalize_stringified_arguments=any(
                                provider.normalize_tool_arguments
                                for provider in _active_providers(startup)
                            ),
                            completion_guard=startup.completion_guard,
                            continue_after_unknown_usage=startup.continue_after_unknown_usage,
                            recover_truncated_replies=True,
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
                            await run_stdio(
                                AgentProtocolServer(service), input_stream, output_stream
                            )


def _native_instructions(
    verified: set[str], profiles: tuple[ProductNativeProcessProfile, ...]
) -> Literal["native", "external_boundary"] | None:
    """按实际验证通过的原生命令后端选择执行指令；未验证的Profile不参与，配置本身不授予权限。

    本机沙箱或沙箱外命令（ADR 0111、0113）用本机命令指令，与外部边界同时验证时仍以其为准，
    外部边界从不得到声称本机沙箱或host_command的指令；只有外部边界命令（ADR 0121）时用外部边界
    指令；没有原生命令时返回None，由调用方按容器命令沿用通用执行指令。
    """
    backends = {profile.backend for profile in profiles if native_tool_name(profile) in verified}
    if backends - {"external_boundary"}:
        return "native"
    return "external_boundary" if backends else None


def _active_profiles(startup: _ProductRuntimeStartup) -> list[ModelProfile]:
    """当前Profile链（含Fallback）的Profile定义，按配置顺序；Context策略与Provider选项由此派生。"""
    chain = set(startup.profile.profile_chain)
    return [profile for profile in startup.config.config.profiles if profile.profile_id in chain]


def _active_providers(startup: _ProductRuntimeStartup) -> tuple[ProviderDefinition, ...]:
    """当前Profile链（含Fallback）实际使用的Provider定义。"""
    used = {profile.provider_id for profile in _active_profiles(startup)}
    return tuple(
        provider for provider in startup.config.config.providers if provider.provider_id in used
    )


def _chain_context_window(chain: list[ModelProfile]) -> int | None:
    """Profile链共用一个Context策略：任一Profile未声明窗口时沿用固定上限，否则取最小窗口。"""
    windows = [profile.context_window_tokens for profile in chain]
    if any(window is None for window in windows):
        return None
    return min(window for window in windows if window is not None)


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
    zero_config_actions: bool = False,
    unattended: bool = False,
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
        zero_config_actions=zero_config_actions and action_config is None,
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
        else load_product_action_config(action_config_path, zero_config=True)
        if request.zero_config_actions
        else load_product_action_config(action_config_path)
    )
    if action_config is None and action_snapshot.config_sha256 != preflight.action_config_sha256:
        raise KernelError("product_action_config_changed", "Product Action配置在预检后发生变化")
    report = diagnose_configuration(loaded, selection, secrets)
    if not report.ready:
        raise KernelError("product_config_diagnostic_failed", "产品配置离线诊断未通过")
    # 只读路径预检先于创建根外锁，拒绝Workspace重叠时不向用户仓库写入锚点。
    await asyncio.to_thread(_check_state_overlap, state_directory, workspace_root)
    with product_state_owner(Path(state_directory)) as owner:
        owner.require_ready(owner.state_root)
        state_root, git_path = await _validated_runtime_paths(
            workspace_root=workspace_root,
            state_directory=owner.state_root,
            git_executable=git_executable,
        )
        owner.require(state_root)
        startup = _ProductRuntimeStartup(
            config=loaded,
            profile=selection,
            actions=action_snapshot,
            workspace_root=workspace_root,
            state_root=state_root,
            git_path=git_path,
            secrets=secrets,
            root_owner=owner,
            completion_guard=unattended or loaded.config.completion_guard,
            # 本地端点没有费用风险；付费交互模式下停止并交给用户决定是否继续。
            continue_after_unknown_usage=(
                unattended or loaded.config.chain_pricing(selection.selected_profile) == "free"
            ),
        )
        await _serve_product_stdio(
            startup,
            input_stream=input_stream,
            output_stream=output_stream,
            expected_active_sha256=expected_active_sha256,
            expected_active_profile=expected_active_profile,
            expected_active_action_sha256=expected_active_action_sha256,
        )
