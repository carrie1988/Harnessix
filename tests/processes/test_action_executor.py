import asyncio
import sys
from datetime import timedelta

import pytest

from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    SecretRef,
    utc_now,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import ProcessActionInput, process_action_tool
from harnessix.processes.contracts import ProcessLimits, ProcessResult
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.storage import SQLiteEffectJournal
from harnessix.worker import ActionWorker
from tests.helpers import action_request
from tests.processes.helpers import cleanup, ready, stopped


def factory(root, *, limits=None, program=None):
    return lambda: HostProcessRuntime(
        root,
        {"python": program or sys.executable},
        limits=limits,
    )


async def service(root, runtime_factory, *, lease_seconds=30, auto_execute=True):
    registry = ToolRegistry()
    registry.register(process_action_tool(runtime_factory))
    value = ActionService(
        journal=SQLiteEffectJournal(root / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        lease_seconds=lease_seconds,
        auto_execute=auto_execute,
    )
    await value.initialize()
    return value


def command(code, *arguments, timeout=5.0, key="process:fixture"):
    return action_request(
        "host.process",
        {
            "program": "python",
            "arguments": ["-I", "-c", code, *arguments],
            "timeout_seconds": timeout,
        },
        idempotency_key=key,
        effect_hint=EffectClass.NON_IDEMPOTENT_WRITE,
    )


async def approve(value, request):
    pending = await value.submit(request)
    assert pending.status is ActionStatus.PENDING_APPROVAL
    return await value.decide_approval(
        request.action_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
    )


async def test_persistent_approval_executes_once_and_records_binary_result(tmp_path):
    marker = tmp_path / "executed"
    value = await service(tmp_path, factory(tmp_path))
    request = command(
        "import os,sys; open(sys.argv[1],'ab').write(b'x'); os.write(1,bytes(range(256)))",
        str(marker),
    )
    try:
        assert not marker.exists()
        completed = await approve(value, request)
        duplicate = await value.submit(
            command(
                "import os,sys; open(sys.argv[1],'ab').write(b'x'); os.write(1,bytes(range(256)))",
                str(marker),
            )
        )
        events = await value.events(request.action_id)
        assert completed.status is ActionStatus.SUCCEEDED
        assert duplicate.request.action_id == request.action_id
        assert marker.read_bytes() == b"x"
        assert completed.result is not None and completed.result.receipt is not None
        result = ProcessResult.model_validate(completed.result.output)
        assert result.stdout.data() == bytes(range(256)) and result.returncode == 0
        assert [event.event_type for event in events][-3:] == [
            "execution_leased",
            "execution_started",
            "execution_completed",
        ]
    finally:
        await value.close()


async def test_rejection_and_binding_drift_do_not_launch(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = await service(tmp_path, factory(tmp_path))
    rejected = command("open(__import__('sys').argv[1],'w').write('bad')", str(marker))
    try:
        await value.submit(rejected)
        denied = await value.decide_approval(
            rejected.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="reviewer-a"),
        )
        assert denied.status is ActionStatus.DENIED and not marker.exists()
    finally:
        await value.close()

    current = [None]
    value = await service(
        tmp_path,
        lambda: HostProcessRuntime(tmp_path, {"python": sys.executable}, environment=current[0]),
    )
    current[0] = {}
    drifted = command("open(__import__('sys').argv[1],'w').write('bad')", str(marker), key="drift")
    try:
        completed = await approve(value, drifted)
        assert completed.status is ActionStatus.FAILED and not marker.exists()
        assert completed.result is not None and completed.result.error is not None
        assert completed.result.error.code == "process_binding_changed"
    finally:
        await value.close()


async def test_ready_approval_rejects_changed_binding_after_restart(tmp_path):
    marker = tmp_path / "must-not-exist"
    request = command(
        "open(__import__('sys').argv[1],'w').write('bad')", str(marker), key="restart-drift"
    )
    before = await service(tmp_path, factory(tmp_path), auto_execute=False)
    try:
        await before.submit(request)
        ready_snapshot = await before.decide_approval(
            request.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
        )
        assert ready_snapshot.status is ActionStatus.READY
    finally:
        await before.close()

    after = await service(
        tmp_path,
        factory(tmp_path, limits=ProcessLimits(stdout_bytes=1)),
        auto_execute=False,
    )
    try:
        completed = await ActionWorker(
            after, poll_seconds=0.01, heartbeat_seconds=1, recovery_interval_seconds=1
        ).run_once()
        assert completed is not None and completed.status is ActionStatus.FAILED
        assert completed.result.error.code == "process_tool_contract_changed"
        assert not marker.exists()
    finally:
        await after.close()


@pytest.mark.parametrize("returncode", [0, 7])
async def test_command_exit_is_result_not_action_transport_failure(tmp_path, returncode):
    value = await service(tmp_path, factory(tmp_path))
    try:
        completed = await approve(value, command(f"raise SystemExit({returncode})"))
        assert completed.status is ActionStatus.SUCCEEDED
        assert ProcessResult.model_validate(completed.result.output).returncode == returncode
    finally:
        await value.close()


async def test_incomplete_pipe_evidence_is_unknown_and_never_replayed(tmp_path):
    value = await service(
        tmp_path,
        factory(tmp_path, limits=ProcessLimits(stop_output_bytes=16384, stdout_bytes=32)),
    )
    request = command("import os\nwhile True: os.write(1,b'x'*8192)")
    try:
        completed = await approve(value, request)
        assert completed.status is ActionStatus.UNKNOWN
        assert completed.result is not None and completed.result.error is not None
        assert completed.result.error.code == "process_effect_unknown"
        evidence = ProcessResult.model_validate(completed.result.output)
        assert evidence.stop_reason == "output_limit" and not evidence.stdout.eof
        reconciled = await value.reconcile(request.action_id)
        assert reconciled.status is ActionStatus.MANUAL_INTERVENTION
        assert reconciled.result.error.code == "reconciliation_not_supported"
    finally:
        await value.close()


async def test_secret_refs_fail_after_approval_without_launch(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = await service(tmp_path, factory(tmp_path))
    request = command("open(__import__('sys').argv[1],'w').write('bad')", str(marker))
    request = request.model_copy(update={"secret_refs": (SecretRef(name="fixture-secret"),)})
    try:
        completed = await approve(value, request)
        assert completed.status is ActionStatus.FAILED and not marker.exists()
        assert completed.result.error.code == "process_secret_refs_unsupported"
    finally:
        await value.close()


async def test_task_cancel_reaps_process_then_lease_recovers_unknown(tmp_path):
    marker = tmp_path / "started"
    value = await service(tmp_path, factory(tmp_path), lease_seconds=1)
    request = command(
        "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)",
        str(marker),
    )
    task = None
    pid = None
    try:
        await value.submit(request)
        task = asyncio.create_task(
            value.decide_approval(
                request.action_id,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
            )
        )
        pid = await ready(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stopped(pid)
        assert (await value.get(request.action_id)).status is ActionStatus.RUNNING
        recovered = await value.journal.recover_expired(utc_now() + timedelta(seconds=2))
        assert recovered == [request.action_id]
        assert (await value.get(request.action_id)).status is ActionStatus.UNKNOWN
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await value.close()
        await cleanup(pid)


@pytest.mark.parametrize(
    "arguments",
    [
        {"program": "python", "arguments": ["-V"], "timeout_seconds": "1"},
        {"program": "python", "arguments": [1]},
        {"program": "python", "arguments": ["nul\0"]},
        {"program": "python", "arguments": ["-V"], "shell": True},
    ],
)
def test_action_input_keeps_strict_json_contract(arguments):
    with pytest.raises(ValueError):
        ProcessActionInput.model_validate(arguments)


def test_binding_fingerprint_covers_execution_authority(tmp_path):
    base = HostProcessRuntime(tmp_path, {"python": sys.executable})
    same = HostProcessRuntime(tmp_path, {"python": sys.executable})
    env = HostProcessRuntime(tmp_path, {"python": sys.executable}, environment={})
    limits = HostProcessRuntime(
        tmp_path, {"python": sys.executable}, limits=ProcessLimits(stdout_bytes=1)
    )
    assert base.binding_fingerprint == same.binding_fingerprint
    assert len(base.binding_fingerprint) == 64
    assert len({base.binding_fingerprint, env.binding_fingerprint, limits.binding_fingerprint}) == 3
    tool = process_action_tool(factory(tmp_path))
    descriptor = tool.descriptor()
    assert descriptor.effect_class is EffectClass.NON_IDEMPOTENT_WRITE
    assert descriptor.requires_approval and descriptor.requires_idempotency
    assert not descriptor.supports_reconciliation
    assert descriptor.version.endswith(base.binding_fingerprint)
    assert sys.executable not in descriptor.version
