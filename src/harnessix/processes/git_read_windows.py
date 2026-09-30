"""Windows固定Git读取：复用持久Process Owner与Job Object，不开放Shell。"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.contracts import (
    MAX_CAPTURE_BYTES,
    ProcessRequest,
    ProcessResult,
    ProcessStream,
)
from harnessix.processes.git_observation import GitBaselineReadResult, validate_git_result
from harnessix.processes.owner_protocol import OutputRedactionSource
from harnessix.processes.owner_receipt import ProcessOwnerReceiptV2
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessLease,
    ProcessOutputObservation,
    ProcessSpec,
)
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.processes.supervisor import WindowsProcessSupervisor
from harnessix.processes.supervisor_capabilities import probe_windows_process_capability
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.git_windows_binding import pin_windows_git, pin_windows_git_state
from harnessix.workspace.snapshot import capture_workspace_snapshot

WINDOWS_GIT_ARGUMENTS = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "color.ui=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=NUL",
    "-c",
    "core.attributesFile=NUL",
    "-c",
    "submodule.recurse=false",
)


def _environment(executable: Path, *, for_delivery: bool = False) -> dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": os.pathsep.join((str(executable.parent), str(Path(system_root) / "System32"))),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "NUL",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": "",
        **({"GIT_NO_LAZY_FETCH": "1"} if for_delivery else {}),
    }


def _stream(body: bytes, observation: ProcessOutputObservation, limit: int) -> ProcessStream:
    prefix = body[:limit]
    return ProcessStream(
        data_base64=base64.b64encode(prefix).decode("ascii"),
        captured_bytes=len(prefix),
        observed_bytes=observation.observed_bytes,
        observed_sha256=observation.sha256,
        truncated=observation.observed_bytes > len(prefix),
        eof=observation.eof,
    )


@dataclass(frozen=True)
class _GitReadConfiguration:
    root: Path
    executable: Path
    state: Path
    capability: ProcessCapabilityProbe
    fingerprint: str
    output_redaction: OutputRedactionSource | None
    for_delivery: bool = False


class WindowsGitReadProcess:
    """只由GitReadRuntime生成固定参数；SQLite Lease与输出存入宿主私有状态。"""

    def __init__(
        self,
        root: Path,
        executable: Path,
        state_directory: Path | None,
        output_redaction: OutputRedactionSource | None = None,
        *,
        for_delivery: bool = False,
    ) -> None:
        with pin_windows_git(root, executable) as binding:
            self.binding_fingerprint = binding.fingerprint
        if state_directory is None or not state_directory.is_absolute():
            raise KernelError("product_git_state_required", "Windows Git读取需要私有状态目录")
        with pin_windows_git_state(state_directory):
            state = state_directory.resolve(strict=False)
        if state.is_relative_to(root) or root.is_relative_to(state):
            raise KernelError("product_state_overlap", "Git状态目录不能与Workspace重叠")
        capability = probe_windows_process_capability()
        self.binding_fingerprint = canonical_digest(
            {
                "native_binding": self.binding_fingerprint,
                "capability": capability.digest,
                "executable": os.path.normcase(str(executable)),
                "state": os.path.normcase(str(state_directory)),
                **({"purpose": "git-delivery-baseline/raw-v2"} if for_delivery else {}),
            }
        )
        self._configuration = _GitReadConfiguration(
            root,
            executable,
            state_directory,
            capability,
            self.binding_fingerprint,
            output_redaction,
            for_delivery,
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("process_closed", "Git只读端口已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True

    async def run(self, request: ProcessRequest, cancel: CancelToken) -> ProcessResult:
        result = await self._drive(request, cancel, baseline=False)
        assert isinstance(result, ProcessResult)
        return result

    async def run_baseline(
        self, request: ProcessRequest, cancel: CancelToken
    ) -> GitBaselineReadResult:
        if not self._configuration.for_delivery:
            raise KernelError("git_baseline_reader_required", "Git基准需要固定交付读取端口")
        result = await self._drive(request, cancel, baseline=True)
        assert isinstance(result, GitBaselineReadResult)
        return result

    async def _drive(
        self, request: ProcessRequest, cancel: CancelToken, *, baseline: bool
    ) -> ProcessResult | GitBaselineReadResult:
        cancel.checkpoint()
        if self._closed or request.program != "git":
            raise KernelError("process_program_denied", "Git只读端口不可执行该请求")
        if request.timeout_seconds > 5.0:
            raise KernelError("process_budget_exceeded", "Git读取期限超过宿主上限")
        operation = CancelToken()
        task = asyncio.create_task(
            _execute_git(self._configuration, request, operation, baseline=baseline)
        )
        try:
            return await cancel.run(asyncio.shield(task))
        except (TurnCancelled, asyncio.CancelledError):
            # 启动线程不接受Task取消。先让Owner接到停机，再排空原任务；
            # 不能在CreateProcess完成前丢失Owner或随后重发命令。
            from harnessix.tools.runtime import _drain

            operation.cancel()
            await _drain(task)
            raise


async def _execute_git(
    config: _GitReadConfiguration,
    request: ProcessRequest,
    cancel: CancelToken,
    *,
    baseline: bool = False,
) -> ProcessResult | GitBaselineReadResult:
    began = time.monotonic()
    with (
        pin_windows_git_state(config.state),
        pin_windows_git(config.root, config.executable) as binding,
    ):
        current = canonical_digest(
            {
                "native_binding": binding.fingerprint,
                "capability": config.capability.digest,
                "executable": os.path.normcase(str(config.executable)),
                "state": os.path.normcase(str(config.state)),
                **({"purpose": "git-delivery-baseline/raw-v2"} if config.for_delivery else {}),
            }
        )
        if current != config.fingerprint:
            raise KernelError("process_binding_changed", "Git只读绑定已变化")
        async with WindowsProcessSupervisor(
            config.state / "process-owner", output_redaction=config.output_redaction
        ) as supervisor:
            environment = _environment(config.executable, for_delivery=config.for_delivery)
            spec = build_process_spec(
                invocation="argv",
                argv=(str(config.executable), *request.arguments),
                timeout_seconds=request.timeout_seconds,
                output_bytes=(9 if config.for_delivery else 8) * MAX_CAPTURE_BYTES,
            )
            plan = _git_read_plan(config, spec, environment)
            # 与正式备份共用原Plan/Process目录；不能留下只有Lease摘要的孤立事实。
            with SQLiteExecutionPlanStore(config.state / "execution-plans.db") as plans:
                plans.save_plan(plan)
            handle = await supervisor.start(
                plan,
                spec,
                config.capability,
                workspace=config.root,
                environment=environment,
            )
            lease = await handle.wait(cancel)
            if lease.stop_reason == "cancelled":
                raise TurnCancelled
            if lease.stop_reason == "timeout":
                raise ReadToolError("timeout")
            if lease.state != "exited" or lease.stop_reason != "exited":
                raise ReadToolError("io_failed")
            if lease.returncode != 0:
                raise ReadToolError("not_found")
            stdout = await handle.output("stdout")
            stderr = await handle.output("stderr")
            assert lease.pid is not None
            result = ProcessResult(
                pid=lease.pid,
                returncode=0,
                stop_reason="exited",
                termination="none",
                stdout=_stream(stdout, lease.stdout, MAX_CAPTURE_BYTES),
                stderr=_stream(stderr, lease.stderr, 16 * 1024),
                elapsed_seconds=time.monotonic() - began,
            )
            validate_git_result(result)
            if not baseline:
                binding.verify()
                return result
            # 由原句柄重验MAC、完整Lease事实和接受序号，不从脱敏统计或终态缓存补raw。
            receipt = await handle._terminal_owner_receipt()  # noqa: SLF001
            if not isinstance(receipt, ProcessOwnerReceiptV2):
                raise KernelError(
                    "git_baseline_raw_observation_required", "Git基准需要原始字节观察证明"
                )
            observed = GitBaselineReadResult(result, receipt.raw_stdout, receipt.raw_stderr)
            binding.verify()
            return observed


def _git_read_plan(
    config: _GitReadConfiguration, spec: ProcessSpec, environment: dict[str, str]
) -> ExecutionPlanV2:
    """宿主授权固定只读查询；与模型任意Process及其审批计划完全分离。"""
    capability = build_capability_evidence_v2(
        platform="windows",
        provider="windows_git_read_owner",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=config.capability.supports_pty,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=config.capability.digest,
    )
    snapshot = capture_workspace_snapshot(config.root, platform="windows")
    return build_execution_plan_v2(
        ExecutionIntent(
            source="builtin",
            source_id="harnessix.git_read",
            tool="git.read",
            tool_version="1",
            tool_fingerprint=config.fingerprint,
            arguments=spec.model_dump(mode="json", warnings="error"),
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            idempotency_key=str(spec.process_id),
        ),
        snapshot,
        environment=environment,
        secrets=(),
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="full",
            capability_digest=capability.evidence_digest,
            profile_digest=config.fingerprint,
        ),
        policy=ExecutionPolicyBinding(
            version="git-read/windows-v1",
            decision=PolicyDecisionKind.ALLOW,
            policy_id="git.read.fixed",
            reason_code="fixed_read_only_command",
        ),
        capabilities=capability,
    )


def _git_read_lease(plan: ExecutionPlanV2, lease: ProcessLease) -> bool:
    return (
        plan.intent.source == "builtin"
        and plan.intent.source_id == "harnessix.git_read"
        and plan.intent.tool == "git.read"
        and plan.intent.effect_class == EffectClass.READ_ONLY
        and plan.fingerprint == lease.plan_fingerprint
    )


def _reject_uncertain_git_reads(state: Path, plans: SQLiteExecutionPlanStore) -> None:
    # UNKNOWN不能因离开active集合而成为下一次启动的豁免。
    with SQLiteProcessLeaseStore(
        state / "process-owner/process-leases.db", read_only=True
    ) as store:
        rows = store._db.execute(  # noqa: SLF001 - 固定只读查询，随后由原Store验真全部合同
            "SELECT process_id FROM process_leases WHERE state='unknown'"
        )
        for (identity,) in rows:
            lease = store.load(UUID(identity))
            plan = plans.load_plan(lease.plan_id)
            if isinstance(plan, ExecutionPlanV2) and _git_read_lease(plan, lease):
                raise KernelError("product_git_recovery_uncertain", "旧Git只读查询未能验真终结")


async def reconcile_windows_git_reads(state: Path) -> None:
    """产品Root Owner持有后只收敛旧只读Receipt；不重放查询、不控制存储中的PID。"""
    if not (state / "process-owner/process-leases.db").exists():
        return
    with SQLiteExecutionPlanStore(state / "execution-plans.db", read_only=True) as plans:
        _reject_uncertain_git_reads(state, plans)
        async with WindowsProcessSupervisor(state / "process-owner") as supervisor:
            pending = []
            for lease in supervisor.active_leases():
                plan = plans.load_plan(lease.plan_id)
                if isinstance(plan, ExecutionPlanV2) and _git_read_lease(plan, lease):
                    pending.append(lease.process_id)
            if len(pending) > 16:
                raise KernelError("product_git_recovery_limit", "旧Git只读查询超过启动恢复上限")
            for identity in pending:
                observed = await supervisor.reconcile(identity)
                if observed.state != "exited":
                    raise KernelError("product_git_recovery_uncertain", "旧Git只读查询未能验真终结")
