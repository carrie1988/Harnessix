"""Git 交付的受控 IO：复用原 Process Owner、审批、回执和持久账本。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue, ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import _GitRunner, git_delivery_implementation_digest
from harnessix.delivery.git_command import GitCommand
from harnessix.delivery.git_material_input_contracts import GitMaterialProof, decode_proof
from harnessix.delivery.git_object_material import (
    GitObjectMaterial,
    GitObjectRead,
    decode_git_object_batch,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionPlanV2,
    execution_is_approved,
)
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.owner_protocol import OutputRedactionSource
from harnessix.processes.owner_receipt import ProcessOwnerReceiptV2
from harnessix.processes.supervision_contracts import (
    MAX_PROCESS_INPUT_BYTES,
    ProcessCapabilityProbe,
    ProcessLease,
    ProcessSpec,
)
from harnessix.processes.supervision_planner import build_host_process_binding, build_process_spec
from harnessix.processes.supervisor import (
    PosixProcessSupervisor,
    SupervisedProcess,
    WindowsProcessSupervisor,
)
from harnessix.processes.supervisor_capabilities import (
    probe_posix_process_capability,
    probe_windows_process_capability,
)
from harnessix.product_config.git_material_process import GitMaterialPreparation
from harnessix.secrets.redaction import secret_patterns
from harnessix.tools.runtime import _drain

_STREAM_BYTES = 1024 * 1024
_INPUT_CHUNK_BYTES = 64 * 1024

if TYPE_CHECKING:
    from harnessix.product_config.git_material_process import GitMaterialExecution


class _GitOutputProtection:
    """同一次执行共用原Owner保护快照，拒绝等字节替换的命中。"""

    def __init__(self, source: OutputRedactionSource) -> None:
        self._source = source
        self._values: tuple[bytes, ...] | None = None

    def output_redaction_values(self) -> tuple[bytes, ...]:
        # 原Supervisor在创建Lease前校验封套；不重复读取可轮换的来源。
        if self._values is None:
            self._values = self._source.output_redaction_values()
        return self._values

    def require_unmatched(self, stdout: bytes, stderr: bytes) -> None:
        if self._values is None:
            raise KernelError("process_output_protection_unavailable", "Git缺少本次执行的保护快照")
        if any(pattern in stdout or pattern in stderr for pattern in secret_patterns(self._values)):
            raise KernelError("git_process_output_changed", "Git输出包含受保护材料")

    def require_input_unmatched(
        self, body: bytes, cancel: CancelToken, budget: GitOperationBudget
    ) -> None:
        """原 Owner 已冻结的同一保护集合须在完整正文落盘及交给 Git 前生效。"""
        if self._values is None:
            raise KernelError("process_output_protection_unavailable", "Git缺少本次执行的保护快照")
        for pattern in secret_patterns(self._values):
            cancel.checkpoint()
            budget.remaining()
            if pattern in body:
                raise KernelError("git_material_input_protected", "Git输入包含受保护材料")


class GitOperationBudget:
    """整个交付操作共享单调期限；每条命令不得重新获得完整预算。"""

    def __init__(self, seconds: float) -> None:
        if (
            type(seconds) not in {float, int}
            or not math.isfinite(seconds)
            or not 0 < seconds <= 3600
        ):
            raise KernelError("git_process_budget_invalid", "Git操作期限无效")
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        """到期明确拒绝，调用方不得自动重置或恢复执行。"""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise KernelError("git_process_timeout", "Git操作总期限已耗尽")
        return remaining

    @property
    def expires_at_monotonic_ns(self) -> int:
        """同一操作的绝对期限；不能把 worker 的启动或子命令当作新预算。"""
        return int(self._deadline * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class PreparedGitProcess:
    """固定命令和新 Process 身份；材料本身不是执行批准。"""

    command: GitCommand = field(repr=False)
    spec: ProcessSpec = field(repr=False)
    capability: ProcessCapabilityProbe = field(repr=False)
    budget: GitOperationBudget = field(repr=False)
    material: GitObjectRead | None = None
    write: GitMaterialExecution | None = field(default=None, repr=False)

    def approval_arguments(self) -> dict[str, JsonValue]:
        """原 Execution Plan 必须绑定完整命令摘要和 stdin 摘要，不持久化正文。"""
        arguments: dict[str, JsonValue] = {
            "version": "git-delivery-process/v1",
            "implementation_digest": _implementation_digest(),
            "command_digest": self.command.digest,
            "process": self.spec.model_dump(mode="json", warnings="error"),
        }
        if self.material is not None:
            arguments["material"] = self.material.binding()
        if self.write is not None:
            arguments["material_input"] = cast(
                JsonValue, json.loads(json.dumps(self.write.binding(), allow_nan=False))
            )
        return arguments


def _implementation_digest() -> str:
    """新批准覆盖适配代码及原领域实现；原 Process 能力另行绑定 Owner。"""
    from harnessix.execution.contracts import canonical_digest

    try:
        return canonical_digest(
            {
                "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "material_adapter": hashlib.sha256(
                    Path(__file__).with_name("git_material_process.py").read_bytes()
                ).hexdigest(),
                "delivery": git_delivery_implementation_digest(),
            }
        )
    except OSError:
        raise KernelError("git_capability_unavailable", "Git受控执行实现不可证明") from None


@dataclass(frozen=True, slots=True)
class GitProcessCompletion:
    """仅返回已认证、完整且未经脱敏改写的字节；不是业务 Commit 成功。"""

    lease: ProcessLease = field(repr=False)
    receipt: ProcessOwnerReceiptV2 = field(repr=False)
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    material: GitObjectMaterial | None = field(default=None, repr=False)
    input_proof: GitMaterialProof | None = field(default=None, repr=False)


def _capability() -> ProcessCapabilityProbe:
    if os.name == "nt":
        return probe_windows_process_capability()
    if os.name == "posix":
        return probe_posix_process_capability()
    raise KernelError("git_platform_unsupported", "当前平台不支持受控Git交付")


class GitDeliveryProcess(GitMaterialPreparation):
    """受信产品内部端口；不构造 ALLOW 计划，不发布模型任意 Git 工具。"""

    def __init__(
        self,
        runner: _GitRunner,
        state_root: Path,
        *,
        output_redaction: OutputRedactionSource | None = None,
    ) -> None:
        if not state_root.is_absolute():
            raise KernelError("git_process_state_invalid", "Git进程状态根必须是绝对路径")
        self._runner = runner
        self._state = state_root
        self._output_redaction = output_redaction
        self._active: tuple[CancelToken, asyncio.Task[GitProcessCompletion]] | None = None
        self._closed = False

    def prepare(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        budget: GitOperationBudget,
        input_data: bytes | None = None,
        index_file: Path | None = None,
        accepted: tuple[int, ...] = (0,),
        timeout: float = 20.0,
    ) -> PreparedGitProcess:
        """生成待绑定材料，不创建 Plan/Lease，不启动进程或自动批准。"""
        return _prepare_process(
            self._runner,
            self._state,
            self._closed,
            cwd,
            arguments,
            budget,
            input_data,
            index_file,
            accepted,
            timeout,
        )

    async def run(
        self,
        prepared: PreparedGitProcess,
        plan: ExecutionPlanV2,
        cancel: CancelToken,
        *,
        budget: GitOperationBudget,
        checkpoint: ExecutionApprovalCheckpoint | None = None,
    ) -> GitProcessCompletion:
        """消费原正式批准；取消包括启动期，并在返回前结算唯一 Owner。"""
        return await _run_process(self, prepared, plan, cancel, budget, checkpoint)

    def prepare_object_read(
        self,
        cwd: Path,
        request: GitObjectRead,
        *,
        budget: GitOperationBudget,
        timeout: float = 20.0,
    ) -> PreparedGitProcess:
        """固定OID用途取得完整材料；控制输入及普通命令额度不变。"""
        _validate_material(request)
        return _prepare_process(
            self._runner,
            self._state,
            self._closed,
            cwd,
            ("cat-file", "--batch"),
            budget,
            (request.object_id + "\n").encode("ascii"),
            None,
            (0,),
            timeout,
            request,
        )

    async def aclose(self) -> None:
        """关闭阻止新调用，活动调用排空后才释放 Owner/SQLite 连接。"""
        self._closed = True
        if self._active is not None:
            operation, task = self._active
            operation.cancel()
            await _drain(task)


def _prepare_process(
    runner: _GitRunner,
    state_root: Path,
    closed: bool,
    cwd: Path,
    arguments: tuple[str, ...],
    budget: GitOperationBudget,
    input_data: bytes | None,
    index_file: Path | None,
    accepted: tuple[int, ...],
    timeout: float,
    material: GitObjectRead | None = None,
) -> PreparedGitProcess:
    """单一规划职责：校验宿主材料并派生原 ProcessSpec，不写业务状态。"""
    if closed:
        raise KernelError("git_process_closed", "Git受控端口已关闭")
    if (
        type(timeout) not in {float, int}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 3600
        or not accepted
        or any(type(code) is not int or not 0 <= code <= 255 for code in accepted)
        or (input_data is not None and type(input_data) is not bytes)
    ):
        raise KernelError("git_process_request_invalid", "Git受控请求不符合契约")
    if input_data is not None and len(input_data) > MAX_PROCESS_INPUT_BYTES:
        # 不放宽原 Process 输入合同。大对象需后继受信材料通道，不能截断后执行。
        raise KernelError("git_process_input_limit", "Git输入超过原Process合同上限")
    command = runner.prepare_command(
        cwd,
        arguments,
        input_data=input_data,
        index_file=index_file,
        accepted=accepted,
        timeout=timeout,
    )
    state = state_root.resolve(strict=False)
    source = cwd.resolve(strict=True)
    if state.is_relative_to(source) or source.is_relative_to(state):
        raise KernelError("product_state_overlap", "Git进程状态与Workspace不能重叠")
    spec = build_process_spec(
        invocation="argv",
        argv=command.argv,
        stdin="pipe" if input_data is not None else "closed",
        input_bytes=max(1, len(input_data)) if input_data is not None else 0,
        timeout_seconds=min(float(timeout), budget.remaining()),
        output_bytes=_stdout_limit(material) + _STREAM_BYTES,
    )
    return PreparedGitProcess(command, spec, _capability(), budget, material)


async def _run_process(
    self: GitDeliveryProcess,
    prepared: PreparedGitProcess,
    plan: ExecutionPlanV2,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: ExecutionApprovalCheckpoint | None,
) -> GitProcessCompletion:
    """协调入口期限、批准和取消；启动线程只由原 Supervisor 控制。"""
    cancel.checkpoint()
    budget.remaining()
    if self._closed:
        raise KernelError("git_process_closed", "Git受控端口已关闭")
    if self._active is not None:
        raise KernelError("git_process_busy", "Git受控端口忙；未隐式排队")
    if prepared.budget is not budget:
        raise KernelError("git_process_budget_mismatch", "Git命令不能更换原操作期限")
    _require_prepared(prepared)
    if prepared.write is not None and self._output_redaction is None:
        raise KernelError("process_output_protection_unavailable", "Git写入缺少原保护来源")
    self._runner.verify_command(prepared.command)
    if prepared.capability != _capability():
        raise KernelError("process_capability_mismatch", "Git进程Owner能力已变化")
    if (
        plan.intent.arguments != prepared.approval_arguments()
        or plan.capabilities.provider_evidence_digest != prepared.capability.digest
        or plan.workspace.cwd != "."
        or plan.secrets
    ):
        raise KernelError("git_process_plan_mismatch", "Git命令与原执行计划不一致")
    if not execution_is_approved(plan, checkpoint):
        raise KernelError("approval_required", "Git执行计划尚未取得有效批准")
    # 与 Supervisor 使用同一绑定校验；不匹配环境在创建私有 Plan/Lease 前拒绝。
    build_host_process_binding(
        plan,
        prepared.spec,
        prepared.capability,
        dict(prepared.command.environment),
        prepared.approval_arguments(),
    )
    # 核验期间可耗尽期限；必须在创建任务前拒绝，不能清空活动引用后遗留执行。
    timeout_seconds = budget.remaining()
    operation = CancelToken()
    task = asyncio.create_task(_execute_process(self, prepared, plan, operation, checkpoint))
    self._active = operation, task
    try:
        async with asyncio.timeout(timeout_seconds):
            return await cancel.run(asyncio.shield(task))
    except TimeoutError:
        operation.cancel()
        await _drain(task)
        _raise_uncertain_settlement(task)
        raise KernelError("git_process_timeout", "Git操作总期限已耗尽") from None
    except (TurnCancelled, asyncio.CancelledError):
        operation.cancel()
        await _drain(task)
        _raise_uncertain_settlement(task)
        raise
    finally:
        self._active = None


def _raise_uncertain_settlement(task: asyncio.Task[GitProcessCompletion]) -> None:
    """取消或超时不得覆盖未知效果、回执损坏或失联控制的强失败语义。"""
    if task.done() and not task.cancelled():
        error = task.exception()
        if isinstance(error, KernelError) and error.code in {
            "git_material_effect_unknown",
            "git_material_stage_changed",
            "git_process_unknown",
            "process_owner_receipt_invalid",
            "process_output_corrupt",
            "process_control_lost",
            "process_owner_token_invalid",
        }:
            code = (
                error.code
                if error.code in {"git_material_effect_unknown", "git_material_stage_changed"}
                else "git_process_unknown"
            )
            raise KernelError(code, "Git停止效果无法验真；禁止自动重放") from None


async def _execute_process(
    self: GitDeliveryProcess,
    prepared: PreparedGitProcess,
    plan: ExecutionPlanV2,
    cancel: CancelToken,
    checkpoint: ExecutionApprovalCheckpoint | None,
) -> GitProcessCompletion:
    command = prepared.command
    cancel.checkpoint()
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    protection = (
        _GitOutputProtection(self._output_redaction) if self._output_redaction is not None else None
    )
    staged = None
    input_sent = False
    try:
        async with supervisor_type(
            self._state / "process-owner", output_redaction=protection
        ) as supervisor:
            # 在启动前固定 Plan；重启时原 Lease 可定位完整意图，不生成孤立进程事实。
            with SQLiteExecutionPlanStore(self._state / "execution-plans.db") as plans:
                plans.save_plan(plan)
            self._runner.verify_command(command)
            handle = await supervisor.start(
                plan,
                prepared.spec,
                prepared.capability,
                workspace=command.cwd,
                environment=dict(command.environment),
                checkpoint=checkpoint,
                intent_arguments=prepared.approval_arguments(),
            )
            try:
                data = command.input_data
                if prepared.write is not None:
                    from harnessix.product_config.git_material_process import prepare_material_stdin

                    assert protection is not None
                    staged = await prepare_material_stdin(
                        prepared.write, protection, cancel, prepared.budget
                    )
                    data = prepared.write.control_input
                if data is not None and not cancel.cancelled:
                    for offset in range(0, len(data), _INPUT_CHUNK_BYTES):
                        if cancel.cancelled:
                            break
                        input_sent = True
                        await handle.send_stdin(data[offset : offset + _INPUT_CHUNK_BYTES])
                    if not cancel.cancelled:
                        await handle.close_stdin()
                lease = await handle.wait(cancel)
                return await _complete_process(self, prepared, handle, lease, protection)
            except BaseException:
                from harnessix.product_config.git_material_process import settle_input_failure

                await settle_input_failure(
                    handle, uncertain=prepared.write is not None and input_sent
                )
                raise
    except BaseException:
        # 整个 Supervisor 退出也在用途边界内；关闭失联不能降级已可能送达的效果。
        if prepared.write is not None and input_sent:
            raise KernelError(
                "git_material_effect_unknown", "Git材料写入未取得完整验真；禁止自动重放"
            ) from None
        raise
    finally:
        if staged is not None:
            try:
                staged.remove()
            except (OSError, KernelError):
                raise KernelError(
                    "git_material_effect_unknown" if input_sent else "git_material_stage_changed",
                    "Git私有材料清理未完成；禁止自动重放",
                ) from None


async def _complete_process(
    self: GitDeliveryProcess,
    prepared: PreparedGitProcess,
    handle: SupervisedProcess,
    lease: ProcessLease,
    protection: _GitOutputProtection | None,
) -> GitProcessCompletion:
    """原认证流核对先于解码；写入证明只认证 worker，不替代独立对象回读。"""
    command = prepared.command
    _require_exit(lease, command.accepted)
    receipt = await handle._terminal_owner_receipt()  # noqa: SLF001 - 原句柄验真端口
    if not isinstance(receipt, ProcessOwnerReceiptV2):
        raise KernelError("git_process_raw_required", "Git命令缺少原始流认证回执")
    stdout, stderr = await handle.output("stdout"), await handle.output("stderr")
    _require_raw_bytes(
        stdout,
        receipt.raw_stdout.observed_bytes,
        receipt.raw_stdout.sha256,
        receipt.raw_stdout.eof,
        limit=_stdout_limit(prepared.material),
    )
    _require_raw_bytes(
        stderr,
        receipt.raw_stderr.observed_bytes,
        receipt.raw_stderr.sha256,
        receipt.raw_stderr.eof,
    )
    if protection is not None:
        protection.require_unmatched(stdout, stderr)
    self._runner.verify_command(command)
    material = decode_git_object_batch(prepared.material, stdout) if prepared.material else None
    proof = None
    if prepared.write is not None:
        prepared.write.verify()
        proof = decode_proof(stdout, prepared.write.request)
        if proof.producer_pid != lease.pid:
            raise KernelError("git_material_proof_invalid", "Git材料生产者身份不一致")
    return GitProcessCompletion(lease, receipt, stdout, stderr, material, proof)


def _require_prepared(prepared: PreparedGitProcess) -> None:
    command, spec = prepared.command, prepared.spec
    try:
        ProcessSpec.model_validate_json(spec.model_dump_json(warnings="error"))
        if prepared.write is not None:
            from harnessix.product_config.git_material_process import require_write

            require_write(prepared)
            return
        _require_material_command(prepared)
        if (
            spec.invocation != "argv"
            or spec.argv != command.argv
            or spec.terminal != "pipe"
            or spec.lifecycle != "foreground"
            or spec.stdin != ("pipe" if command.input_data is not None else "closed")
            or spec.input_bytes
            != (max(1, len(command.input_data)) if command.input_data is not None else 0)
            or spec.timeout_seconds > command.timeout_seconds
            or spec.output_bytes != _stdout_limit(prepared.material) + _STREAM_BYTES
        ):
            raise ValueError
    except (ValidationError, ValueError, TypeError):
        raise KernelError("git_process_request_invalid", "Git派生进程材料不符合原命令") from None


def _require_exit(lease: ProcessLease, accepted: tuple[int, ...]) -> None:
    if lease.state == "unknown":
        raise KernelError("git_process_unknown", "Git进程终态未知；禁止自动重放")
    if lease.stop_reason == "cancelled":
        raise TurnCancelled
    if lease.stop_reason == "timeout":
        raise KernelError("git_process_timeout", "Git固定命令期限已耗尽")
    if lease.state != "exited" or lease.stop_reason != "exited" or lease.returncode not in accepted:
        raise KernelError("git_command_failed", "Git固定命令失败或未取得正常退出")


def _require_raw_bytes(
    body: bytes, observed: int, digest: str, eof: bool, *, limit: int = _STREAM_BYTES
) -> None:
    if observed > limit:
        raise KernelError("git_command_failed", "Git固定命令输出超过原上限")
    if not eof or observed != len(body) or hashlib.sha256(body).hexdigest() != digest:
        raise KernelError("git_process_output_changed", "Git输出缺失、截断或已受保护变换")


def _validate_material(request: GitObjectRead) -> None:
    """重新验证冻结实例，避免类型伪造借用途取得更大捕获额度。"""
    if type(request) is not GitObjectRead:
        raise KernelError("git_material_request_invalid", "Git对象读取请求无效")
    request.__post_init__()


def _stdout_limit(material: GitObjectRead | None) -> int:
    if material is None:
        return _STREAM_BYTES
    _validate_material(material)
    return material.stdout_limit


def _require_material_command(prepared: PreparedGitProcess) -> None:
    """大结果用途仅绑定唯一对象命令；普通命令不能升级为任意捕获。"""
    if prepared.material is None:
        return
    _validate_material(prepared.material)
    command = prepared.command
    if (
        command.arguments != ("cat-file", "--batch")
        or command.input_data != (prepared.material.object_id + "\n").encode("ascii")
        or command.accepted != (0,)
        or command.index_file is not None
        or command.allowed_protocols != ("file",)
        or dict(command.environment).get("GIT_NO_LAZY_FETCH") != "1"
    ):
        raise ValueError
