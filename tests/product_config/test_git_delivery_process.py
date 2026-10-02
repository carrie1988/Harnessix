"""真实受控 Git IO 的独立验证；不将进程合同视作完整 Git 业务验收。"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import _GitRunner
from harnessix.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    PolicyDecisionKind,
    RiskLevel,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import (
    bind_environment,
    build_capability_evidence_v2,
    build_execution_plan_v2,
)
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.owner_protocol import OutputRedactionSource
from harnessix.processes.owner_receipt import (
    OwnerReceipt,
    ProcessOwnerReceiptV2,
    read_owner_receipt,
    verify_owner_receipt,
)
from harnessix.processes.supervision_contracts import (
    MAX_PROCESS_INPUT_BYTES,
    ProcessLease,
    ProcessSpec,
    process_spec_digest,
)
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.product_config import git_delivery_process as git_process
from harnessix.product_config.git_delivery_process import (
    GitDeliveryProcess,
    GitOperationBudget,
    GitProcessCompletion,
    PreparedGitProcess,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原平台 Process Owner")

_MIB = 1024 * 1024
_MARKER_PROGRAM = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'started')"
_PID_FILES = ("parent.pid", "child.pid", "grandchild.pid")
_INTERPRETER_IDENTITY_PROGRAM = (
    "import json,os,sys; print(json.dumps({'pid':os.getpid(),'executable':sys.executable}))"
)


def _assert_interpreter_identity(executable: Path, root_pid: int, stdout: str) -> None:
    identity = json.loads(stdout)
    assert identity["pid"] == root_pid, "解释器 worker PID 与启动 PID 不一致"
    assert Path(identity["executable"]).resolve(strict=True) == executable, "解释器路径绑定不一致"


def _verified_base_interpreter() -> Path:
    # Windows venv redirector 会再启动 worker，不能把它的 PID 等同于 worker 自报 PID。
    executable = Path(sys._base_executable).resolve(strict=True)
    with subprocess.Popen(
        (str(executable), "-I", "-c", _INTERPRETER_IDENTITY_PROGRAM),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError("基础解释器身份探测超时") from None
        assert process.returncode == 0, stderr
        _assert_interpreter_identity(executable, process.pid, stdout)
    return executable


@dataclass
class _ProcessCase:
    workspace: Path
    state: Path
    runner: _GitRunner
    port: GitDeliveryProcess


@pytest.fixture
async def make_process(
    tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Callable[..., _ProcessCase]]:
    cases: list[_ProcessCase] = []
    mode = request.config.getoption("--git-material-trace2", default="off")

    def create(
        *,
        python_code: str | None = None,
        executable: Path | None = None,
        output_redaction: OutputRedactionSource | None = None,
    ) -> _ProcessCase:
        root = tmp_path / f"case-{len(cases)}"
        workspace = root / "source-workspace"
        workspace.mkdir(parents=True)
        if executable is None:
            if python_code is not None:
                executable = _verified_base_interpreter()
            else:
                git = shutil.which("git")
                if git is None:
                    pytest.skip("需要真实 Git 可执行文件")
                executable = Path(git)
        runner = _GitRunner(executable.resolve(strict=True), root / "runner-private")
        if python_code is not None:
            # 仅替换测试实例的固定前缀；生产端口和原平台 Owner 保持真实实现。
            runner._global = ("-I", "-c", python_code)
            runner._binding = replace(runner._binding, global_arguments=runner._global)
        state = root / "process-private"
        case = _ProcessCase(
            workspace,
            state,
            runner,
            GitDeliveryProcess(
                runner, state, output_redaction=output_redaction, material_trace2_mode=mode
            ),
        )
        cases.append(case)
        return case

    try:
        yield create
    finally:
        for case in reversed(cases):
            await case.port.aclose()


def _plan(
    case: _ProcessCase,
    prepared: PreparedGitProcess,
    *,
    decision: PolicyDecisionKind = PolicyDecisionKind.REQUIRE_APPROVAL,
    environment: dict[str, str] | None = None,
    cwd: str = ".",
) -> ExecutionPlanV2:
    # 沿用 Supervisor 的 V2 构建方式；参数、环境和能力均绑定 prepare 的真实返回值。
    probe = prepared.capability
    capability = build_capability_evidence_v2(
        platform=probe.platform,
        provider="windows_process_owner" if probe.platform == "windows" else "posix_process_owner",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=probe.supports_pty,
        supports_background=probe.supports_background,
        supports_process_tree=probe.supports_process_tree,
        provider_evidence_digest=probe.digest,
    )
    snapshot = capture_workspace_snapshot(
        case.workspace,
        cwd=cwd,
        resources=(WorkspaceResourceRequest(path=".", access="write"),),
        platform=probe.platform,
    )
    return build_execution_plan_v2(
        ExecutionIntent(
            source="builtin",
            source_id="harnessix",
            tool="process.supervised",
            tool_version="v1",
            tool_fingerprint="c" * 64,
            arguments=prepared.approval_arguments(),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            idempotency_key=str(prepared.spec.process_id),
        ),
        snapshot,
        environment=dict(prepared.command.environment) if environment is None else environment,
        secrets=(),
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="full",
            capability_digest=capability.evidence_digest,
            profile_digest="b" * 64,
        ),
        policy=ExecutionPolicyBinding(
            version="process/v1",
            decision=decision,
            policy_id="git.process.test",
            reason_code="test",
        ),
        capabilities=capability,
    )


def _checkpoint(
    plan: ExecutionPlanV2, outcome: ApprovalOutcome = ApprovalOutcome.APPROVED
) -> ExecutionApprovalCheckpoint:
    return ExecutionApprovalCheckpoint(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        decision=ApprovalRecord(
            outcome=outcome,
            actor="git-process-test-reviewer",
            request_fingerprint=plan.fingerprint,
        ),
    )


async def _run(case: _ProcessCase, prepared: PreparedGitProcess) -> GitProcessCompletion:
    plan = _plan(case, prepared)
    return await case.port.run(
        prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
    )


def _assert_not_started(case: _ProcessCase, *markers: Path) -> None:
    # 不打开写连接，避免负向校验本身创建 Plan、Lease 或 Owner 状态。
    assert not case.state.exists()
    assert not (case.state / "execution-plans.db").exists()
    assert not (case.state / "process-owner").exists()
    for marker in markers:
        assert not marker.exists()


def _lease(case: _ProcessCase, prepared: PreparedGitProcess) -> ProcessLease:
    path = case.state / "process-owner/process-leases.db"
    assert path.is_file()
    with SQLiteProcessLeaseStore(path, read_only=True) as store:
        lease = store.load(prepared.spec.process_id)
        assert not store.active()
    return lease


def _owner_receipt(case: _ProcessCase, prepared: PreparedGitProcess) -> OwnerReceipt:
    lease = _lease(case, prepared)
    return read_owner_receipt(
        case.state / "process-owner/runs" / str(lease.process_id) / "receipt.json",
        owner_token=lease.owner_token,
        process_id=lease.process_id,
        owner_identity=lease.owner_identity,
    )


def _receipt(case: _ProcessCase, prepared: PreparedGitProcess) -> ProcessOwnerReceiptV2:
    receipt = _owner_receipt(case, prepared)
    assert isinstance(receipt, ProcessOwnerReceiptV2)
    return receipt


def _assert_completion(
    case: _ProcessCase,
    prepared: PreparedGitProcess,
    completion: GitProcessCompletion,
    stdout: bytes,
    stderr: bytes,
) -> None:
    lease, receipt = completion.lease, completion.receipt
    assert lease.state == "exited" and lease.stop_reason == "exited"
    assert lease.returncode in prepared.command.accepted
    assert lease.process_id == prepared.spec.process_id
    assert lease.process_spec_digest == prepared.spec.digest
    assert lease.capability_digest == prepared.capability.digest
    assert _lease(case, prepared) == lease
    assert _receipt(case, prepared) == receipt
    assert (
        verify_owner_receipt(
            receipt,
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=lease.owner_identity,
        )
        == receipt
    )
    assert receipt.state == "exited" and receipt.pid == lease.pid
    assert receipt.returncode == lease.returncode and receipt.stop_reason == lease.stop_reason
    assert completion.stdout == stdout and completion.stderr == stderr
    for stream, expected in (("stdout", stdout), ("stderr", stderr)):
        raw = getattr(receipt, f"raw_{stream}")
        observed = getattr(lease, stream)
        digest = hashlib.sha256(expected).hexdigest()
        assert raw.eof and raw.observed_bytes == len(expected) and raw.sha256 == digest
        assert observed.eof and not observed.truncated
        assert observed.persisted_bytes == observed.observed_bytes == len(expected)
        assert observed.persisted_sha256 == observed.sha256 == digest
    with SQLiteExecutionPlanStore(case.state / "execution-plans.db", read_only=True) as plans:
        plan = plans.load_plan(lease.plan_id)
    assert isinstance(plan, ExecutionPlanV2)
    assert plan.intent.arguments == prepared.approval_arguments()
    assert plan.environment == bind_environment(
        dict(prepared.command.environment), platform=prepared.capability.platform
    )
    assert plan.fingerprint == lease.plan_fingerprint


def _changed_spec(spec: ProcessSpec, **changes: object) -> ProcessSpec:
    candidate = spec.model_copy(update=changes)
    candidate = candidate.model_copy(update={"digest": process_spec_digest(candidate)})
    return ProcessSpec.model_validate_json(candidate.model_dump_json(warnings="error"))


def _process_running(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87, "无法核实进程状态"
            return False
        try:
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            result = kernel32.WaitForSingleObject(handle, 0)
            assert result in {0, 258}, "无法核实进程退出"
            return result == 258
        finally:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux 的已退出孤儿可能暂留僵尸，不能把僵尸误计为仍在运行。
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
        except FileNotFoundError:
            return False
    return True


async def _wait_stopped(pids: tuple[int, ...]) -> None:
    for _ in range(500):
        running = await asyncio.gather(*(asyncio.to_thread(_process_running, pid) for pid in pids))
        remaining = tuple(pid for pid, alive in zip(pids, running, strict=True) if alive)
        if not remaining:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"进程树尚未收回: {remaining}")


def _emergency_stop(pid: int) -> None:
    # 仅在用例清理时兜底终止本用例的已知 PID；正式断言在兜底之前完成。
    if not _process_running(pid):
        return
    if os.name == "posix":
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(1, False, pid)
    if handle:
        try:
            kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateProcess(handle, 9)
        finally:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle(handle)


def _tree_program() -> str:
    common = (
        "import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "if os.name == 'posix': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    )
    grandchild = common + "Path('grandchild.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n"
    child = (
        common
        + "Path('child.pid').write_text(str(os.getpid()))\n"
        + f"subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}])\n"
        + "time.sleep(60)\n"
    )
    return (
        common
        + "if len(sys.argv) > 1: Path(sys.argv[1]).write_bytes(b'started')\n"
        + "Path('parent.pid').write_text(str(os.getpid()))\n"
        + f"subprocess.Popen([sys.executable, '-I', '-c', {child!r}])\n"
        + "time.sleep(60)\n"
    )


def _marked_pids(workspace: Path) -> tuple[int, ...]:
    pids: list[int] = []
    for name in _PID_FILES:
        try:
            value = (workspace / name).read_text()
        except FileNotFoundError:
            continue
        if value.isdecimal():
            pids.append(int(value))
    return tuple(pids)


@asynccontextmanager
async def _live_tree(
    make_process: Callable[..., _ProcessCase],
    *,
    command_timeout: float = 20.0,
    budget_seconds: float = 45.0,
    delay_before_run: float = 0.0,
) -> AsyncIterator[
    tuple[
        _ProcessCase,
        PreparedGitProcess,
        asyncio.Task[GitProcessCompletion],
        CancelToken,
        tuple[int, ...],
    ]
]:
    case = make_process(python_code=_tree_program())
    prepared = case.port.prepare(
        case.workspace, (), budget=GitOperationBudget(budget_seconds), timeout=command_timeout
    )
    plan = _plan(case, prepared)
    if delay_before_run:
        await asyncio.sleep(delay_before_run)
    cancel = CancelToken()
    task = asyncio.create_task(
        case.port.run(prepared, plan, cancel, budget=prepared.budget, checkpoint=_checkpoint(plan))
    )
    try:
        for _ in range(500):
            pids = await asyncio.to_thread(_marked_pids, case.workspace)
            if len(pids) == len(_PID_FILES):
                assert all(_process_running(pid) for pid in pids)
                break
            if task.done():
                await task
                pytest.fail("进程树未就绪即退出")
            await asyncio.sleep(0.01)
        else:
            pytest.fail("真实进程树未报告就绪")
        yield case, prepared, task, cancel, pids
    finally:
        cancel.cancel()
        await case.port.aclose()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pid in reversed(_marked_pids(case.workspace)):
            await asyncio.to_thread(_emergency_stop, pid)


def test_python_fixture_binds_verified_base_interpreter(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    prepared = case.port.prepare(case.workspace, ("started",), budget=GitOperationBudget(45))
    assert Path(prepared.command.argv[0]) == Path(sys._base_executable).resolve(strict=True)
    assert prepared.spec.argv == prepared.command.argv
    _assert_not_started(case)


@pytest.mark.parametrize("identity_fault", ["forwarder", "different-executable"])
def test_python_fixture_probe_rejects_nonmatching_process_identity(
    tmp_path: Path, identity_fault
) -> None:
    executable = Path(sys._base_executable).resolve(strict=True)
    arguments = (str(executable), "-I", "-c", _INTERPRETER_IDENTITY_PROGRAM)
    if identity_fault == "forwarder":
        # 真实转发进程另启解释器；stdout 来自 worker，Popen.pid 仍属于 launcher。
        forwarder = (
            "import subprocess,sys; "
            "raise SystemExit(subprocess.call([sys.executable,*sys.argv[1:]]))"
        )
        arguments = (str(executable), "-I", "-c", forwarder, *arguments[1:])
    with subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and not stderr
        if identity_fault == "forwarder":
            assert json.loads(stdout)["pid"] != process.pid
            expected_executable, reason = executable, "worker PID 与启动 PID 不一致"
        else:
            assert json.loads(stdout)["pid"] == process.pid
            expected_executable = tmp_path / "different-python"
            expected_executable.write_bytes(b"not-the-selected-interpreter")
            reason = "解释器路径绑定不一致"
        with pytest.raises(AssertionError, match=reason):
            _assert_interpreter_identity(expected_executable, process.pid, stdout)


async def test_real_git_version_matches_synchronous_runner_and_v2_raw_receipt(make_process) -> None:
    case = make_process()
    baseline = case.runner.run(case.workspace, ("--version",))
    prepared = case.port.prepare(case.workspace, ("--version",), budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    assert baseline.stdout.startswith(b"git version ")
    assert completion.lease.returncode == baseline.returncode == 0
    _assert_completion(case, prepared, completion, baseline.stdout, baseline.stderr)


async def test_deadline_exhausted_by_preflight_never_creates_an_execution_task(
    make_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    original = git_process.build_host_process_binding
    execute = git_process._execute_process
    tasks: list[asyncio.Task] = []

    def expire_at_last_preflight(*args, **kwargs):
        binding = original(*args, **kwargs)
        # 精确故障注入只消耗共享期限，不替换后继真实Owner执行或批准校验。
        prepared.budget._deadline = time.monotonic() - 1
        return binding

    async def observe_real_execution(*args, **kwargs):
        tasks.append(asyncio.current_task())
        return await execute(*args, **kwargs)

    monkeypatch.setattr(git_process, "build_host_process_binding", expire_at_last_preflight)
    monkeypatch.setattr(git_process, "_execute_process", observe_real_execution)
    try:
        with pytest.raises(KernelError) as failure:
            await case.port.run(
                prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
            )
        assert failure.value.code == "git_process_timeout"
        await asyncio.sleep(0)
        await case.port.aclose()
        assert not tasks, "期限核验失败后不得创建无人管理的真实执行任务"
        _assert_not_started(case, marker)
    finally:
        # 负对照实现可能留下真实任务；诊断退出前等其自然结算，不拿此清理当验收。
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("cwd", ["nested", "nested/deeper"])
async def test_approved_nonroot_workspace_cwd_cannot_change_actual_git_directory(
    make_process, cwd
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    subdirectory = case.workspace / cwd
    subdirectory.mkdir(parents=True)
    prepared = case.port.prepare(case.workspace, ("started",), budget=GitOperationBudget(45))
    plan = _plan(case, prepared, cwd=cwd)
    assert plan.workspace.cwd == cwd
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_plan_mismatch"
    _assert_not_started(case, case.workspace / "started", subdirectory / "started")


@pytest.mark.parametrize(
    "size", [0, 65537, _MIB], ids=["empty", "65537-chunk-boundary", "1MiB-limit"]
)
async def test_real_git_binary_hash_object_stdin_matches_synchronous_runner(
    make_process, size
) -> None:
    case = make_process()
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    arguments = ("hash-object", "--stdin")
    baseline = case.runner.run(case.workspace, arguments, input_data=body)
    prepared = case.port.prepare(
        case.workspace, arguments, budget=GitOperationBudget(45), input_data=body
    )
    assert prepared.spec.stdin == "pipe" and prepared.spec.input_bytes == max(1, size)
    expected = hashlib.sha1(f"blob {size}\0".encode() + body).hexdigest().encode() + b"\n"
    assert baseline.stdout == expected
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, baseline.stdout, baseline.stderr)


def test_prepare_has_no_approval_or_durable_execution_side_effects(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    budget = GitOperationBudget(45)
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=budget)
    assert prepared.budget is budget
    assert prepared.command.argv == prepared.spec.argv
    assert prepared.capability.platform == ("windows" if os.name == "nt" else "posix")
    _assert_not_started(case, marker)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [(b"\0one", b"\0two"), (None, b"")],
    ids=["same-length-different-binary", "closed-versus-empty-pipe"],
)
async def test_same_argv_different_stdin_changes_approval_and_cannot_reuse_plan(
    make_process, original, replacement
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    budget = GitOperationBudget(45)
    first = case.port.prepare(case.workspace, (str(marker),), budget=budget, input_data=original)
    second = case.port.prepare(
        case.workspace, (str(marker),), budget=budget, input_data=replacement
    )
    # 对齐 Process ID，排除随机身份造成参数不同的假阳性。
    second = replace(second, spec=_changed_spec(second.spec, process_id=first.spec.process_id))
    assert first.command.argv == second.command.argv
    assert first.command.digest != second.command.digest
    assert first.approval_arguments() != second.approval_arguments()
    if original is not None:
        assert first.spec == second.spec
    plan = _plan(case, first)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            second, plan, CancelToken(), budget=budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_plan_mismatch"
    _assert_not_started(case, marker)


@pytest.mark.parametrize("approval", ["missing", "rejected", "deny", "another-plan"])
async def test_unapproved_or_rejected_execution_creates_no_plan_lease_or_marker(
    make_process, approval
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    decision = (
        PolicyDecisionKind.DENY if approval == "deny" else PolicyDecisionKind.REQUIRE_APPROVAL
    )
    plan = _plan(case, prepared, decision=decision)
    checkpoint = None
    if approval == "rejected":
        checkpoint = _checkpoint(plan, ApprovalOutcome.REJECTED)
    elif approval == "deny":
        checkpoint = _checkpoint(plan)
    elif approval == "another-plan":
        checkpoint = _checkpoint(_plan(case, prepared))
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
        )
    assert failure.value.code == "approval_required"
    _assert_not_started(case, marker)


@pytest.mark.parametrize("phase", ["prepare", "run"])
async def test_expired_operation_budget_creates_no_plan_lease_or_marker(
    make_process, phase
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    budget = GitOperationBudget(0.5)
    if phase == "run":
        prepared = case.port.prepare(case.workspace, (str(marker),), budget=budget)
        plan = _plan(case, prepared)
    await asyncio.sleep(0.55)
    with pytest.raises(KernelError) as failure:
        if phase == "prepare":
            case.port.prepare(case.workspace, (str(marker),), budget=budget)
        else:
            await case.port.run(
                prepared, plan, CancelToken(), budget=budget, checkpoint=_checkpoint(plan)
            )
    assert failure.value.code == "git_process_timeout"
    _assert_not_started(case, marker)


async def test_fresh_budget_cannot_replace_prepared_operation_deadline(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared,
            plan,
            CancelToken(),
            budget=GitOperationBudget(45),
            checkpoint=_checkpoint(plan),
        )
    assert failure.value.code == "git_process_budget_mismatch"
    _assert_not_started(case, marker)


@pytest.mark.parametrize("field", ["argv", "stdin"])
async def test_replaced_spec_with_valid_recomputed_digest_is_rejected_before_start(
    make_process, field
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker, substituted = case.workspace / "started", case.workspace / "substituted"
    prepared = case.port.prepare(
        case.workspace, (str(marker),), budget=GitOperationBudget(45), input_data=b"exact-input"
    )
    if field == "argv":
        spec = _changed_spec(prepared.spec, argv=(*prepared.spec.argv[:-1], str(substituted)))
    else:
        spec = _changed_spec(prepared.spec, stdin="closed", input_bytes=0)
    tampered = replace(prepared, spec=spec)
    assert tampered.spec.digest == process_spec_digest(tampered.spec)
    # 连审批计划也重建，验证拒绝来自命令派生合同，而非旧指纹碰巧不匹配。
    plan = _plan(case, tampered)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            tampered, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_request_invalid"
    _assert_not_started(case, marker, substituted)


@pytest.mark.parametrize("change", ["omit", "modify"])
async def test_plan_environment_mismatch_is_rejected_before_private_plan_or_lease(
    make_process, change
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    environment = dict(prepared.command.environment)
    if change == "omit":
        environment.pop("HOME")
    else:
        environment["HOME"] += "-different"
    plan = _plan(case, prepared, environment=environment)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "process_capability_mismatch"
    _assert_not_started(case, marker)


async def test_fixed_runner_command_drift_blocks_start(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    case.runner._global = ("-I", "-c", _MARKER_PROGRAM + "; print('different')")
    case.runner._binding = replace(case.runner._binding, global_arguments=case.runner._global)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_command_binding_changed"
    _assert_not_started(case, marker)


async def test_replaced_cwd_identity_blocks_start(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    original = case.workspace.with_name("original-workspace")
    case.workspace.rename(original)
    case.workspace.mkdir()
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_command_binding_changed"
    _assert_not_started(case, marker, original / "started")


async def test_replaced_executable_identity_blocks_start(make_process, tmp_path: Path) -> None:
    executable = tmp_path / ("private-python.exe" if os.name == "nt" else "private-python")
    source = await asyncio.to_thread(Path(sys.executable).resolve)
    await asyncio.to_thread(shutil.copy2, source, executable)
    case = make_process(python_code=_MARKER_PROGRAM, executable=executable)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    executable.rename(executable.with_name("original-python"))
    await asyncio.to_thread(shutil.copy2, source, executable)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_executable_changed"
    _assert_not_started(case, marker)


async def test_precancelled_token_does_not_create_execution_state(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(TurnCancelled):
        await case.port.run(
            prepared, plan, cancel, budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    _assert_not_started(case, marker)


@pytest.mark.parametrize("cancellation", ["token", "task"])
async def test_running_cancellation_reclaims_parent_child_and_grandchild(
    make_process, cancellation
) -> None:
    async with _live_tree(make_process) as (case, prepared, task, cancel, pids):
        if cancellation == "token":
            cancel.cancel()
            with pytest.raises(TurnCancelled):
                await asyncio.wait_for(asyncio.shield(task), 10)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 10)
        await _wait_stopped(pids)
        lease = _lease(case, prepared)
        assert lease.state == "exited" and lease.stop_reason == "cancelled"
        assert lease.pid == pids[0]
        assert lease.process_id == prepared.spec.process_id
        assert lease.process_spec_digest == prepared.spec.digest
        assert lease.capability_digest == prepared.capability.digest
        receipt = _receipt(case, prepared)
        assert receipt.state == "exited" and receipt.stop_reason == "cancelled"
        assert receipt.pid == lease.pid and receipt.returncode == lease.returncode
        assert (
            verify_owner_receipt(
                receipt,
                owner_token=lease.owner_token,
                process_id=lease.process_id,
                owner_identity=lease.owner_identity,
            )
            == receipt
        )
        for stream in ("stdout", "stderr"):
            raw, observed = getattr(receipt, f"raw_{stream}"), getattr(lease, stream)
            assert raw.eof and observed.eof and not observed.truncated
            assert raw.observed_bytes == observed.observed_bytes == observed.persisted_bytes
            assert raw.sha256 == observed.sha256 == observed.persisted_sha256


async def test_command_timeout_reclaims_the_real_process_tree(make_process) -> None:
    async with _live_tree(make_process, command_timeout=3.0) as (case, prepared, task, _, pids):
        with pytest.raises(KernelError) as failure:
            await asyncio.wait_for(asyncio.shield(task), 10)
        assert failure.value.code == "git_process_timeout"
        await _wait_stopped(pids)
        lease = _lease(case, prepared)
        assert lease.state == "exited" and lease.stop_reason == "timeout"
        assert _owner_receipt(case, prepared).stop_reason == "timeout"


async def test_shared_budget_timeout_reclaims_tree_without_resetting_at_launch(
    make_process,
) -> None:
    async with _live_tree(make_process, budget_seconds=3.0, delay_before_run=0.25) as (
        case,
        prepared,
        task,
        _,
        pids,
    ):
        with pytest.raises(KernelError) as failure:
            await asyncio.wait_for(asyncio.shield(task), 10)
        assert failure.value.code == "git_process_timeout"
        await _wait_stopped(pids)
        # 总期限比新 Owner 的相对期限先到，停止原因应来自操作取消。
        lease = _lease(case, prepared)
        assert lease.state == "exited" and lease.stop_reason == "cancelled"


async def test_busy_port_rejects_without_queueing_a_second_command(make_process) -> None:
    async with _live_tree(make_process) as (case, prepared, task, cancel, pids):
        marker = case.workspace / "second-started"
        second = case.port.prepare(case.workspace, (str(marker),), budget=prepared.budget)
        plan = _plan(case, second)
        with pytest.raises(KernelError) as failure:
            await case.port.run(
                second, plan, CancelToken(), budget=second.budget, checkpoint=_checkpoint(plan)
            )
        assert failure.value.code == "git_process_busy"
        assert not marker.exists()
        with SQLiteExecutionPlanStore(case.state / "execution-plans.db", read_only=True) as plans:
            with pytest.raises(KernelError) as missing:
                plans.load_plan(plan.plan_id)
        assert missing.value.code == "execution_plan_not_found"
        cancel.cancel()
        with pytest.raises(TurnCancelled):
            await task
        await _wait_stopped(pids)
        with SQLiteProcessLeaseStore(
            case.state / "process-owner/process-leases.db", read_only=True
        ) as leases:
            with pytest.raises(KernelError) as missing:
                leases.load(second.spec.process_id)
        assert missing.value.code == "process_lease_not_found"


async def test_closed_port_rejects_prepare_and_run_without_start(make_process) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    await case.port.aclose()
    await case.port.aclose()
    with pytest.raises(KernelError) as failure:
        case.port.prepare(case.workspace, (str(marker),), budget=prepared.budget)
    assert failure.value.code == "git_process_closed"
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_closed"
    _assert_not_started(case, marker)


async def test_close_reclaims_active_tree_before_returning(make_process) -> None:
    async with _live_tree(make_process) as (case, prepared, task, _, pids):
        await asyncio.wait_for(case.port.aclose(), 10)
        await _wait_stopped(pids)
        with pytest.raises(TurnCancelled):
            await task
        assert _lease(case, prepared).stop_reason == "cancelled"
        with pytest.raises(KernelError) as failure:
            case.port.prepare(case.workspace, (), budget=prepared.budget)
        assert failure.value.code == "git_process_closed"


async def test_raw_binary_stdout_and_stderr_match_legacy_and_authenticated_receipt(
    make_process,
) -> None:
    stdout = bytes(range(256)) * 257 + b"\0\xff\r\nstdout"
    stderr = bytes(reversed(range(256))) * 257 + b"\xff\0\r\nstderr"
    code = (
        f"import sys; sys.stdout.buffer.write({stdout[:256]!r} * 257 + b'\\0\\xff\\r\\nstdout'); "
        f"sys.stderr.buffer.write({stderr[:256]!r} * 257 + b'\\xff\\0\\r\\nstderr')"
    )
    case = make_process(python_code=code)
    baseline = case.runner.run(case.workspace, ())
    assert baseline.stdout == stdout and baseline.stderr == stderr
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, baseline.stdout, baseline.stderr)


@dataclass(frozen=True)
class _Redaction:
    value: bytes

    def output_redaction_values(self) -> tuple[bytes, ...]:
        return (self.value,)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_redaction_rewrite_is_rejected_instead_of_returned_as_git_bytes(
    make_process, stream
) -> None:
    protected = b"low-sensitivity-redaction-canary"
    raw = b"prefix:" + protected + b":suffix\0\xff"
    case = make_process(
        python_code=f"import sys; sys.{stream}.buffer.write({raw!r})",
        output_redaction=_Redaction(protected),
    )
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_process_output_changed"
    lease, receipt = _lease(case, prepared), _receipt(case, prepared)
    assert lease.state == "exited" and lease.returncode == 0
    observation = getattr(receipt, f"raw_{stream}")
    assert observation.eof and observation.observed_bytes == len(raw)
    assert observation.sha256 == hashlib.sha256(raw).hexdigest()
    persisted = (
        case.state / "process-owner/runs" / str(lease.process_id) / f"{stream}.bin"
    ).read_bytes()
    assert persisted == raw.replace(protected, b"[REDACTED]")
    assert hashlib.sha256(persisted).hexdigest() != observation.sha256


async def test_unmatched_redaction_does_not_change_the_raw_result(make_process) -> None:
    case = make_process(
        python_code="import sys; sys.stdout.buffer.write(b'unrelated\\0\\xff')",
        output_redaction=_Redaction(b"absent-canary"),
    )
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, b"unrelated\0\xff", b"")


async def test_exact_one_mib_per_stream_is_complete_not_truncated(make_process) -> None:
    case = make_process(
        python_code=(
            f"import sys; sys.stdout.buffer.write(b'o'*{_MIB}); "
            f"sys.stderr.buffer.write(b'e'*{_MIB})"
        )
    )
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, b"o" * _MIB, b"e" * _MIB)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_one_mib_plus_one_stream_is_rejected_even_with_normal_exit(
    make_process, stream
) -> None:
    raw = b"x" * (_MIB + 1)
    case = make_process(python_code=f"import sys; sys.{stream}.buffer.write(b'x'*{len(raw)})")
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_command_failed"
    lease, receipt = _lease(case, prepared), _receipt(case, prepared)
    assert lease.state == "exited" and lease.stop_reason == "exited" and lease.returncode == 0
    observation = getattr(receipt, f"raw_{stream}")
    assert observation.eof and observation.observed_bytes == len(raw)
    assert observation.sha256 == hashlib.sha256(raw).hexdigest()
    assert not getattr(lease, stream).truncated


async def test_owner_total_output_limit_stops_running_process_and_rejects_prefix(
    make_process,
) -> None:
    case = make_process(python_code="import os\nwhile True: os.write(1, b'x'*32768)")
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_command_failed"
    lease = _lease(case, prepared)
    assert lease.state == "exited" and lease.stop_reason == "output_limit"
    assert lease.stdout.truncated and lease.stdout.eof
    assert lease.stdout.persisted_bytes == 2 * _MIB
    assert lease.pid is not None
    await _wait_stopped((lease.pid,))


@pytest.mark.parametrize("size", [_MIB + 1, 8 * _MIB], ids=["1MiB-plus-one", "8MiB-business-gap"])
async def test_current_process_stdin_limit_rejects_full_git_object_without_truncation(
    make_process, size
) -> None:
    case = make_process()
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    assert MAX_PROCESS_INPUT_BYTES == _MIB
    assert len(body) > MAX_PROCESS_INPUT_BYTES
    arguments = ("hash-object", "--stdin")
    # 同步 Git 能处理完整正文，不代表现有 Process 端口已具备 8MiB 业务材料通道。
    baseline = case.runner.run(case.workspace, arguments, input_data=body)
    expected = hashlib.sha1(f"blob {size}\0".encode() + body).hexdigest().encode() + b"\n"
    assert baseline.stdout == expected and baseline.stderr == b""
    before = hashlib.sha256(body).hexdigest()
    with pytest.raises(KernelError) as failure:
        case.port.prepare(case.workspace, arguments, budget=GitOperationBudget(45), input_data=body)
    assert failure.value.code == "git_process_input_limit"
    assert len(body) == size and hashlib.sha256(body).hexdigest() == before
    _assert_not_started(case)


@pytest.mark.parametrize(
    "replacement_plan", [False, True], ids=["same-plan", "new-plan-same-process-id"]
)
async def test_process_id_cannot_be_replayed_even_by_a_new_port(
    make_process, replacement_plan
) -> None:
    code = "from pathlib import Path; p=Path('executions'); p.open('ab').write(b'x')"
    case = make_process(python_code=code)
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    plan = _plan(case, prepared)
    completion = await case.port.run(
        prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
    )
    _assert_completion(case, prepared, completion, b"", b"")
    assert (case.workspace / "executions").read_bytes() == b"x"
    await case.port.aclose()
    if replacement_plan:
        plan = _plan(case, prepared)
    reopened = GitDeliveryProcess(case.runner, case.state)
    try:
        with pytest.raises(KernelError) as failure:
            await reopened.run(
                prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
            )
    finally:
        await reopened.aclose()
    assert failure.value.code == (
        "process_lease_conflict" if replacement_plan else "process_already_exists"
    )
    assert (case.workspace / "executions").read_bytes() == b"x"
    assert _lease(case, prepared) == completion.lease
    assert list((case.state / "process-owner/runs").iterdir()) == [
        case.state / "process-owner/runs" / str(prepared.spec.process_id)
    ]


@pytest.mark.parametrize("outer_stop", ["timeout", "token", "task"])
@pytest.mark.parametrize(
    "settlement_error",
    [
        "git_process_unknown",
        "process_owner_receipt_invalid",
        "process_output_corrupt",
        "process_control_lost",
        "process_owner_token_invalid",
    ],
)
async def test_mock_uncertain_settlement_overrides_outer_timeout_or_cancellation(
    make_process, monkeypatch: pytest.MonkeyPatch, outer_stop, settlement_error
) -> None:
    case = make_process(python_code=_MARKER_PROGRAM)
    marker = case.workspace / "started"
    prepared = case.port.prepare(case.workspace, (str(marker),), budget=GitOperationBudget(1))
    plan = _plan(case, prepared)
    entered, settled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def uncertain_task(port, prepared_process, execution_plan, operation, checkpoint):
        nonlocal calls
        calls += 1
        entered.set()
        # 只模拟结算任务，不模拟 Supervisor、Owner、启动或进程树回收证据。
        try:
            await operation.run(asyncio.Event().wait())
        except TurnCancelled:
            pass
        settled.set()
        raise KernelError(settlement_error, "测试结算结果无法验真")

    monkeypatch.setattr(git_process, "_execute_process", uncertain_task)
    cancel = CancelToken()
    task = asyncio.create_task(
        case.port.run(prepared, plan, cancel, budget=prepared.budget, checkpoint=_checkpoint(plan))
    )
    try:
        await asyncio.wait_for(entered.wait(), 0.75)
        if outer_stop == "token":
            cancel.cancel()
        elif outer_stop == "task":
            task.cancel()
        with pytest.raises(KernelError) as failure:
            await asyncio.wait_for(asyncio.shield(task), 5)
        assert failure.value.code == "git_process_unknown"
        assert settled.is_set() and calls == 1
        _assert_not_started(case, marker)
    finally:
        cancel.cancel()
        await case.port.aclose()
        await asyncio.gather(task, return_exceptions=True)
