"""已认证闭合终态的纯宿主封存与回收；不读取Git、模型、Keychain或Docker。

调用方先认证原生TerminalTurn并核验状态和补丁。本模块绑定原始证据，先
持久化签名封存和marker，再回收owned工作区内容，保留Native历史及费用。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    is_swebench_isolated_provider_failure,
    is_swebench_quality_failure,
)
from harnessix.evals.swebench.execution_io import read_case_execution_state
from harnessix.evals.swebench.run_config import SweBenchRunConfig
from harnessix.evals.swebench.workspace import _validated_case
from harnessix.evals.swebench.workspace_retirement_contracts import (
    _MAX_SEAL_BYTES,
    _MAX_STATE_BYTES,
    _SESSION_FILE,
    _STATE_FILE,
    _canonical,
    _invalid,
    _key,
    _marker_body,
    _patch_bytes,
    _seal_body,
    _signature,
    _unique_object,
)
from harnessix.evals.swebench.workspace_retirement_contracts import (
    RETIRED_WORKSPACE_MARKER as RETIRED_WORKSPACE_MARKER,
)
from harnessix.evals.swebench.workspace_retirement_contracts import (
    SEALED_WORKSPACE_FILE as SEALED_WORKSPACE_FILE,
)
from harnessix.evals.swebench.workspace_retirement_contracts import (
    SweBenchWorkspaceRetirementManifest as SweBenchWorkspaceRetirementManifest,
)
from harnessix.evals.swebench.workspace_retirement_fs import (
    _assert_directories,
    _Directory,
    _file,
    _FileEvidence,
    _opened,
    _present,
    _publish,
    _recover_publication_alias,
    _reject_unpublished_alias,
    _Scope,
    _stamp,
    _walk,
)


def _scope(plan: SweBenchPreRegistrationPlan, config: SweBenchRunConfig, index: int) -> _Scope:
    if (
        not isinstance(plan, SweBenchPreRegistrationPlan)
        or not isinstance(config, SweBenchRunConfig)
        or type(index) is not int
        or not 0 <= index < len(plan.cases)
    ):
        raise _invalid()
    # 重验model_copy/model_construct输入，不因类型正确而信任已绕过验证的值。
    SweBenchPreRegistrationPlan.model_validate_json(plan.model_dump_json(), strict=True)
    checked = type(config).model_validate_json(config.model_dump_json(), strict=True)
    if checked != config:
        raise _invalid()
    config.require_plan(plan)
    instance_id, _, _ = _validated_case(plan.cases[index])
    root = Path(config.work_root)
    if not root.is_absolute() or ".." in root.parts:
        raise _invalid()
    scope = _Scope(root, root / "cases" / f"case-{index:03d}", root / "workspaces" / instance_id)
    for boundary in (Path(config.source_root), Path(config.cache_root), scope.case_root):
        if scope.workspace.is_relative_to(boundary) or boundary.is_relative_to(scope.workspace):
            raise _invalid()
    return scope


def _terminal(state: SweBenchCaseExecutionState) -> None:
    if not (
        state.status == "completed"
        or is_swebench_quality_failure(state)
        or is_swebench_isolated_provider_failure(state)
    ):
        raise _invalid()


def _evidence(
    scope: _Scope,
    directories: dict[Path, _Directory],
    plan: SweBenchPreRegistrationPlan,
) -> tuple[SweBenchCaseExecutionState, _FileEvidence, _FileEvidence]:
    if set(directories) != set(scope.directories):
        raise _invalid()
    _assert_directories(directories)
    case = directories[scope.case_root]
    state_file = _file(case, _STATE_FILE, _MAX_STATE_BYTES)
    state = read_case_execution_state(scope.case_root / _STATE_FILE, plan)
    _terminal(state)
    if _file(case, _STATE_FILE, _MAX_STATE_BYTES) != state_file:
        raise _invalid()
    session = _file(directories[scope.case_root / "run"], _SESSION_FILE, None)
    _assert_directories(directories)
    return state, state_file, session


def _read_seal(directory: _Directory, key: bytes) -> SweBenchWorkspaceRetirementManifest:
    body = _file(directory, SEALED_WORKSPACE_FILE, _MAX_SEAL_BYTES).body
    payload = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or type(payload.get("hmac_sha256")) is not str:
        raise _invalid()
    signature = payload["hmac_sha256"]
    unsigned = {name: value for name, value in payload.items() if name != "hmac_sha256"}
    # 在解释路径或证据字段前，先认证规范payload。
    if not hmac.compare_digest(signature, _signature(unsigned, key)):
        raise _invalid()
    if body != _canonical(payload) + b"\n":
        raise _invalid()
    return SweBenchWorkspaceRetirementManifest.model_validate_json(body, strict=True)


def _require_manifest_scope(
    manifest: SweBenchWorkspaceRetirementManifest,
    scope: _Scope,
    directories: dict[Path, _Directory],
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    index: int,
) -> None:
    """封存必须属于原计划、配置、路径及当前固定的目录身份。"""
    case = plan.cases[index]
    if (
        manifest.plan_sha256 != plan.plan_sha256
        or manifest.config_fingerprint != config.fingerprint
        or manifest.case_index != index
        or manifest.instance_id != case.instance_id
        or manifest.base_commit != case.base_commit
        or manifest.work_root != str(scope.root)
        or manifest.case_root != str(scope.case_root)
        or manifest.workspace_path != str(scope.workspace)
        or manifest.directory_identities
        != {str(path): directory.identity for path, directory in directories.items()}
    ):
        raise _invalid()


def _require_original_evidence(
    manifest: SweBenchWorkspaceRetirementManifest,
    state: SweBenchCaseExecutionState,
    state_file: _FileEvidence,
    session: _FileEvidence,
) -> None:
    """原 Native 和 State 的身份、全文摘要及 Case 归属必须保持不变。"""
    if (
        manifest.case_state_identity != state_file.identity
        or manifest.session_identity != session.identity
        or manifest.case_state_file_sha256 != state_file.sha256
        or manifest.session_sha256 != session.sha256
        or manifest.state_sha256 != state.state_sha256
        or state.instance_id != manifest.instance_id
        or state.base_commit != manifest.base_commit
    ):
        raise _invalid()


def _require_patch_and_run(
    manifest: SweBenchWorkspaceRetirementManifest,
    state: SweBenchCaseExecutionState,
    patch: bytes,
) -> None:
    """补丁全文及运行身份必须绑定原终态，不能形成第二次尝试。"""
    if (
        manifest.run_id != state.run_id
        or manifest.thread_id != state.thread_id
        or manifest.turn_id != state.turn_id
        or manifest.patch_bytes != len(patch)
        or manifest.patch_sha256 != hashlib.sha256(patch).hexdigest()
        or manifest.patch_sha256 != state.patch_sha256
    ):
        raise _invalid()


def _verify(
    manifest: SweBenchWorkspaceRetirementManifest,
    scope: _Scope,
    directories: dict[Path, _Directory],
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    index: int,
) -> tuple[SweBenchCaseExecutionState, _FileEvidence, _FileEvidence]:
    """按归属、原证据和补丁运行三个独立边界核验封存。"""
    state, state_file, session = _evidence(scope, directories, plan)
    patch = _patch_bytes(manifest.patch)
    _require_manifest_scope(manifest, scope, directories, plan, config, index)
    _require_original_evidence(manifest, state, state_file, session)
    _require_patch_and_run(manifest, state, patch)
    return state, state_file, session


def _verify_marker(directory: _Directory, manifest: SweBenchWorkspaceRetirementManifest) -> None:
    if _file(directory, RETIRED_WORKSPACE_MARKER, 64 * 1024).body != _marker_body(manifest):
        raise _invalid()


def _publish_terminal_seal(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    index: int,
    scope: _Scope,
    directories: dict[Path, _Directory],
    state: SweBenchCaseExecutionState,
    state_file: _FileEvidence,
    session: _FileEvidence,
    patch: str,
    key: bytes,
) -> SweBenchWorkspaceRetirementManifest:
    """完整原事实只封存一次；原子发布前固定路径及文件身份。"""
    unsigned = {
        "spec_version": "harnessix.swebench-workspace-retirement/v1",
        "plan_sha256": plan.plan_sha256,
        "config_fingerprint": config.fingerprint,
        "case_index": index,
        "instance_id": state.instance_id,
        "base_commit": state.base_commit,
        "work_root": str(scope.root),
        "case_root": str(scope.case_root),
        "workspace_path": str(scope.workspace),
        "directory_identities": {
            str(path): directory.identity.model_dump() for path, directory in directories.items()
        },
        "case_state_identity": state_file.identity.model_dump(),
        "session_identity": session.identity.model_dump(),
        "case_state_file_sha256": state_file.sha256,
        "session_sha256": session.sha256,
        "state_sha256": state.state_sha256,
        "run_id": str(state.run_id),
        "thread_id": str(state.thread_id),
        "turn_id": str(state.turn_id),
        "patch": patch,
        "patch_sha256": state.patch_sha256,
        "patch_bytes": len(_patch_bytes(patch)),
    }
    manifest = SweBenchWorkspaceRetirementManifest.model_validate_json(
        _canonical({**unsigned, "hmac_sha256": _signature(unsigned, key)}),
        strict=True,
    )
    _assert_directories(directories)
    _publish(directories[scope.case_root], SEALED_WORKSPACE_FILE, _seal_body(manifest))
    return manifest


def _remove_retired_contents(
    manifest: SweBenchWorkspaceRetirementManifest,
    scope: _Scope,
    directories: dict[Path, _Directory],
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    index: int,
    key: bytes,
    token: CancelToken,
    fault: Callable[[str], None] | None,
) -> None:
    """先全树预检，再只清空工作区内容；每个删除边界仍受原证据约束。"""
    case_directory = directories[scope.case_root]
    workspace = directories[scope.workspace]
    seal_file = _file(case_directory, SEALED_WORKSPACE_FILE, _MAX_SEAL_BYTES)
    if _read_seal(case_directory, key) != manifest:
        raise _invalid()
    _, state_file, session = _verify(manifest, scope, directories, plan, config, index)
    marker_file = _file(workspace, RETIRED_WORKSPACE_MARKER, 64 * 1024)
    if marker_file.body != _marker_body(manifest):
        raise _invalid()

    # 两份发布正文及完整原证据均认证后才回收别名；更新删除阶段的链接数时间戳。
    token.checkpoint()
    _assert_directories(directories)
    seal_file = _recover_publication_alias(case_directory, SEALED_WORKSPACE_FILE, seal_file)
    marker_file = _recover_publication_alias(workspace, RETIRED_WORKSPACE_MARKER, marker_file)

    def checkpoint() -> None:
        token.checkpoint()
        _assert_directories(directories)
        for directory, name, expected in (
            (workspace, RETIRED_WORKSPACE_MARKER, marker_file),
            (case_directory, SEALED_WORKSPACE_FILE, seal_file),
            (case_directory, _STATE_FILE, state_file),
            (directories[scope.case_root / "run"], _SESSION_FILE, session),
        ):
            if _stamp(os.stat(name, dir_fd=directory.fd, follow_symlinks=False)) != expected.stamp:
                raise _invalid()

    # 删除前全树预检。清理期间只检查已认证文件的元数据，避免每个条目
    # 重序列化完整补丁；清理闭合时再复核全文摘要。
    for clean in (False, True):
        _walk(
            workspace.fd,
            workspace.identity.device,
            clean=clean,
            checkpoint=checkpoint,
            fault=fault,
        )
    checkpoint()
    _verify(manifest, scope, directories, plan, config, index)
    if os.listdir(workspace.fd) != [RETIRED_WORKSPACE_MARKER]:
        raise _invalid()
    os.fsync(workspace.fd)


def seal_and_retire_workspace(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    case_index: int,
    state: SweBenchCaseExecutionState,
    patch: str,
    *,
    api_key: str,
    cancel: CancelToken | None = None,
    fault: Callable[[str], None] | None = None,
) -> SweBenchWorkspaceRetirementManifest:
    """先封存再标记，幂等清空本工作区；三阶段故障均可凭原封存恢复。

    已标记的半清理目录不依赖Git；仍须重验原始Native证据、状态和文件身份。
    """
    try:
        scope = _scope(plan, config, case_index)
        key = _key(api_key)
        patch_body = _patch_bytes(patch)
        if not isinstance(state, SweBenchCaseExecutionState):
            raise _invalid()
        token = cancel or CancelToken()
        token.checkpoint()
        with _opened(scope) as directories:
            observed, state_file, session = _evidence(scope, directories, plan)
            if (
                observed != state
                or state.instance_id != plan.cases[case_index].instance_id
                or state.base_commit != plan.cases[case_index].base_commit
                or state.patch_sha256 != hashlib.sha256(patch_body).hexdigest()
            ):
                raise _invalid()
            case_directory = directories[scope.case_root]
            workspace = directories[scope.workspace]
            _reject_unpublished_alias(case_directory, SEALED_WORKSPACE_FILE)
            _reject_unpublished_alias(workspace, RETIRED_WORKSPACE_MARKER)
            if _present(case_directory, SEALED_WORKSPACE_FILE):
                manifest = _read_seal(case_directory, key)
                _verify(manifest, scope, directories, plan, config, case_index)
                if manifest.patch != patch:
                    raise _invalid()
            else:
                if _present(workspace, RETIRED_WORKSPACE_MARKER):
                    raise _invalid()
                manifest = _publish_terminal_seal(
                    plan,
                    config,
                    case_index,
                    scope,
                    directories,
                    state,
                    state_file,
                    session,
                    patch,
                    key,
                )
            if fault is not None:
                fault("after_seal")
            token.checkpoint()
            if _read_seal(case_directory, key) != manifest:
                raise _invalid()
            _verify(manifest, scope, directories, plan, config, case_index)
            if _present(workspace, RETIRED_WORKSPACE_MARKER):
                _verify_marker(workspace, manifest)
            else:
                _publish(workspace, RETIRED_WORKSPACE_MARKER, _marker_body(manifest))
            if fault is not None:
                fault("after_marker")
            _remove_retired_contents(
                manifest, scope, directories, plan, config, case_index, key, token, fault
            )
            return manifest
    except (OSError, ValueError, TypeError, RecursionError):
        raise _invalid() from None


def read_retired_patch(
    plan: SweBenchPreRegistrationPlan,
    config: SweBenchRunConfig,
    case_index: int,
    *,
    api_key: str,
) -> str | None:
    """认证原封存后读取全文，合法空补丁返回空串；不读取Git。"""
    try:
        scope = _scope(plan, config, case_index)
        key = _key(api_key)
        with _opened(scope) as directories:
            case = directories.get(scope.case_root)
            workspace = directories.get(scope.workspace)
            _reject_unpublished_alias(case, SEALED_WORKSPACE_FILE)
            _reject_unpublished_alias(workspace, RETIRED_WORKSPACE_MARKER)
            if not _present(case, SEALED_WORKSPACE_FILE):
                if _present(workspace, RETIRED_WORKSPACE_MARKER):
                    raise _invalid()
                return None
            if case is None:
                raise _invalid()
            manifest = _read_seal(case, key)
            _verify(manifest, scope, directories, plan, config, case_index)
            if _present(workspace, RETIRED_WORKSPACE_MARKER):
                if workspace is None:
                    raise _invalid()
                _verify_marker(workspace, manifest)
            return manifest.patch
    except (OSError, ValueError, TypeError, RecursionError):
        raise _invalid() from None
