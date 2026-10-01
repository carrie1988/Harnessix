"""Git 完整材料输入的宿主装配；正式批准和进程所有权仍由原端口负责。"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import _GitRunner, git_delivery_implementation_digest
from harnessix.delivery.git_contracts import GitRepositoryBinding
from harnessix.delivery.git_identity import _executable_identity, _identity, _path_sha256
from harnessix.delivery.git_material_input_contracts import (
    MAX_MANIFEST_BYTES,
    GitMaterialInput,
    encode_manifest,
    implementation_digest,
)
from harnessix.delivery.git_material_worker import capture_control_files
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.store import _prepare_directory
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.tools.runtime import _drain

if TYPE_CHECKING:
    from harnessix.processes.supervisor import SupervisedProcess
    from harnessix.product_config.git_delivery_process import (
        GitOperationBudget,
        PreparedGitProcess,
        _GitOutputProtection,
    )


class _GitMaterialHost(Protocol):
    """只使用原宿主已有的命令材料、私有根及关闭状态，不取得执行批准。"""

    _runner: _GitRunner
    _state: Path
    _closed: bool


class GitMaterialPreparation:
    """原受控端口的专用准备职责；所有执行仍经同一个 run 和 Supervisor。"""

    _runner: _GitRunner
    _state: Path
    _closed: bool

    def prepare_object_write(
        self,
        cwd: Path,
        repository: GitRepositoryBinding,
        common_directory: Path,
        material: GitObjectMaterial,
        *,
        source_digest: str,
        budget: GitOperationBudget,
        timeout: float = 20.0,
    ) -> PreparedGitProcess:
        """固定完整对象输入用途；不批准 Ref/Commit，也不替代来源和业务登记。"""
        return prepare_object_write(
            self, cwd, repository, common_directory, material, source_digest, budget, timeout
        )


def _worker_argv(request: GitMaterialInput) -> tuple[str, ...]:
    """固定基础解释器和安装包入口，拒绝工作区模块及 venv PID 跳板。"""
    executable = sys.__dict__.get("_base_executable", sys.executable)
    if type(executable) is not str:
        raise KernelError("git_material_binding_changed", "Git输入解释器绑定无效")
    runtime = Path(executable).resolve(strict=True)
    import_root = Path(__file__).resolve().parents[2]
    bootstrap = (
        f"import runpy,sys;sys.path.insert(0,{str(import_root)!r});"
        "runpy.run_module('harnessix.delivery.git_material_worker',run_name='__main__')"
    )
    return (
        str(runtime),
        "-I",
        "-c",
        bootstrap,
        "--manifest-sha256",
        hashlib.sha256(encode_manifest(request)).hexdigest(),
        "--nonce",
        request.nonce,
        "--expiry-monotonic-ns",
        str(request.expiry_monotonic_ns),
    )


@dataclass(frozen=True, slots=True)
class GitMaterialExecution:
    """完整不可变输入与小握手分开绑定，不把子程序证明冒充原始 Git 输入回执。"""

    request: GitMaterialInput
    material: GitObjectMaterial = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    runtime_identity: str
    control_input: bytes = field(repr=False)

    def binding(self) -> dict[str, object]:
        return {
            "version": "git-object-write-execution/v1",
            "request": self.request.binding(),
            "runtime_identity": self.runtime_identity,
            "control_sha256": hashlib.sha256(self.control_input).hexdigest(),
            "control_bytes": len(self.control_input),
        }

    def verify(self) -> None:
        """启动前重验所有输入；观察绑定不是抵御任意同用户安装篡改的 OS 封印。"""
        if (
            type(self.request) is not GitMaterialInput
            or type(self.material) is not GitObjectMaterial
        ):
            raise KernelError("git_material_binding_changed", "Git输入材料绑定已经变化")
        self.request.__post_init__()
        self.material.__post_init__()
        if (
            self.argv != _worker_argv(self.request)
            or _executable_identity(Path(self.argv[0])) != self.runtime_identity
            or implementation_digest() != self.request.implementation_digest
            or encode_manifest(self.request) != self.control_input
            or _identity(Path(self.request.stage_root), directory=True)
            != self.request.stage_root_identity
            or _identity(Path(self.request.repo_path), directory=True) != self.request.repo_identity
            or _identity(Path(self.request.common_path), directory=True)
            != self.request.common_identity
            or _identity(Path(self.request.objects_path), directory=True)
            != self.request.objects_identity
            or self.material.body_sha256 != self.request.body_sha256
            or self.material.body_bytes != self.request.body_bytes
            or self.material.object_id != self.request.expected_oid
            or self.material.object_type != self.request.object_type
            or self.material.object_format != self.request.object_format
            or capture_control_files(self.request.repo_path, self.request.common_path)
            != self.request.control_files
        ):
            raise KernelError("git_material_binding_changed", "Git输入材料绑定已经变化")


def prepare_object_write(
    owner: _GitMaterialHost,
    cwd: Path,
    repository: GitRepositoryBinding,
    common_directory: Path,
    material: GitObjectMaterial,
    source_digest: str,
    budget: GitOperationBudget,
    timeout: float,
) -> PreparedGitProcess:
    """只生成原批准所需材料；不创建正文、Plan、Lease 或 Git 对象。"""
    from harnessix.product_config.git_delivery_process import PreparedGitProcess, _capability

    if owner._closed:
        raise KernelError("git_process_closed", "Git受控端口已关闭")
    try:
        if type(material) is not GitObjectMaterial or type(repository) is not GitRepositoryBinding:
            raise ValueError
        material.__post_init__()
        GitRepositoryBinding.model_validate_json(repository.model_dump_json(warnings="error"))
        cwd, common = cwd.resolve(strict=True), common_directory.resolve(strict=True)
        state = owner._state.resolve(strict=False)
        if state.is_relative_to(cwd) or cwd.is_relative_to(state):
            raise KernelError("product_state_overlap", "Git进程状态与Workspace不能重叠")
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
            or repository.root_identity != _identity(cwd, directory=True)
            or repository.root_path_sha256 != _path_sha256(cwd)
            or repository.common_directory_identity != _identity(common, directory=True)
            or repository.common_directory_sha256 != _path_sha256(common)
            or repository.object_format != material.object_format
            or repository.git_executable_identity != owner._runner.identity
            or repository.implementation_digest != git_delivery_implementation_digest()
        ):
            raise ValueError
        command = owner._runner.prepare_command(
            cwd,
            (
                f"--git-dir={common}",
                "hash-object",
                "--no-filters",
                "-t",
                material.object_type,
                "-w",
                "--stdin",
            ),
            input_data=material.body,
            timeout=timeout,
        )
        stage_root = owner._runner._temp
        nonce = os.urandom(32).hex()
        request = GitMaterialInput.create(
            nonce=nonce,
            source_digest=source_digest,
            stage_root=str(stage_root),
            stage_root_identity=_identity(stage_root, directory=True),
            body_ref=f"body-{nonce}.bin",
            body_path=str(stage_root / f"body-{nonce}.bin"),
            body_sha256=material.body_sha256,
            body_bytes=material.body_bytes,
            expected_oid=material.object_id,
            object_format=material.object_format,
            object_type=material.object_type,
            repo_path=str(cwd),
            repo_identity=repository.root_identity,
            common_path=str(common),
            common_identity=repository.common_directory_identity,
            objects_path=str(common / "objects"),
            objects_identity=_identity(common / "objects", directory=True),
            control_files=capture_control_files(str(cwd), str(common)),
            git_argv=command.argv,
            git_environment=command.environment,
            git_executable_identity=command.executable_identity,
            expiry_monotonic_ns=budget.expires_at_monotonic_ns,
            implementation_digest=implementation_digest(),
        )
        control = encode_manifest(request)
        argv = _worker_argv(request)
        write = GitMaterialExecution(
            request,
            material,
            argv,
            _executable_identity(Path(argv[0])),
            control,
        )
        write.verify()
        spec = build_process_spec(
            invocation="argv",
            argv=argv,
            stdin="pipe",
            input_bytes=len(control),
            timeout_seconds=min(float(timeout), budget.remaining()),
            output_bytes=2 * 1024 * 1024,
        )
        return PreparedGitProcess(command, spec, _capability(), budget, write=write)
    except (OSError, ValueError, TypeError):
        raise KernelError("git_material_input_invalid", "Git受信材料写入请求无效") from None


@dataclass(frozen=True, slots=True)
class StagedGitMaterial:
    path: Path = field(repr=False)
    identity: str

    def remove(self) -> None:
        """仅删除本次创建且物理身份相同的材料，不清扫其他目录或陌生文件。"""
        if _identity(self.path, directory=False) != self.identity:
            raise KernelError("git_material_stage_changed", "Git临时材料身份已经变化")
        self.path.unlink()


def stage_material(
    write: GitMaterialExecution, cancel: CancelToken, budget: GitOperationBudget
) -> StagedGitMaterial:
    """在原 Owner 保护快照核验后使用，完整正文先落私有文件再发送小握手。"""
    write.verify()
    cancel.checkpoint()
    budget.remaining()
    root = Path(write.request.stage_root)
    _prepare_directory(root)
    _prepare_directory(Path(dict(write.request.git_environment)["HOME"]))
    _prepare_directory(Path(write.request.git_argv[6].split("=", 1)[1]))
    path = Path(write.request.body_path)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        body = memoryview(write.material.body)
        while body:
            cancel.checkpoint()
            budget.remaining()
            written = os.write(descriptor, body[:65536])
            if written <= 0:
                raise OSError
            body = body[written:]
        os.fsync(descriptor)
        return StagedGitMaterial(path, _identity(path, directory=False))
    except BaseException:
        # 文件为本次 O_EXCL 创建，尚未发送握手，不能启动 Git；仅回收此文件。
        os.close(descriptor)
        descriptor = -1
        path.unlink()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


async def prepare_material_stdin(
    write: GitMaterialExecution,
    protection: _GitOutputProtection,
    cancel: CancelToken,
    budget: GitOperationBudget,
) -> StagedGitMaterial:
    """线程阶段也归原调用所有；直接任务取消须排空，不遗留后台正文写入。"""

    def prepare() -> StagedGitMaterial:
        protection.require_input_unmatched(write.material.body, cancel, budget)
        return stage_material(write, cancel, budget)

    task = asyncio.create_task(asyncio.to_thread(prepare))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.cancel()
        await _drain(task)
        if not task.cancelled() and task.exception() is None:
            task.result().remove()
        raise


async def settle_input_failure(handle: SupervisedProcess, *, uncertain: bool) -> None:
    """控制通道本身失联也不能覆盖已可能送达输入的业务不确定性。"""
    try:
        await handle.stop("cancelled")
        await handle.wait()
    finally:
        if uncertain:
            raise KernelError(
                "git_material_effect_unknown", "Git材料写入未取得完整验真；禁止自动重放"
            ) from None


def require_write(prepared: PreparedGitProcess) -> None:
    write = prepared.write
    if type(write) is not GitMaterialExecution:
        raise ValueError
    write.verify()
    command, spec = prepared.command, prepared.spec
    request = write.request
    if (
        prepared.material is not None
        or spec.invocation != "argv"
        or spec.argv != write.argv
        or spec.terminal != "pipe"
        or spec.lifecycle != "foreground"
        or spec.stdin != "pipe"
        or spec.input_bytes != len(write.control_input)
        or spec.input_bytes > MAX_MANIFEST_BYTES
        or spec.timeout_seconds > command.timeout_seconds
        or spec.output_bytes != 2 * 1024 * 1024
        or request.git_argv != command.argv
        or request.git_environment != command.environment
        or request.expiry_monotonic_ns != prepared.budget.expires_at_monotonic_ns
        or command.input_data != write.material.body
        or command.accepted != (0,)
        or command.index_file is not None
        or command.allowed_protocols != ("file",)
    ):
        raise ValueError
