"""真实Owner启动握手异常只回收本次新句柄，保持其他共享调用和原故障语义。"""

import asyncio
import os
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import (
    PosixProcessSupervisor,
    SupervisedProcess,
    WindowsProcessSupervisor,
)
from harnessix.product_config import git_delivery_process as delivery
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.state_owner import product_state_owner
from harnessix.secrets.publication import SecretPublicationScope
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs

make_process = inputs.make_process
pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原生Process Owner")


@pytest.mark.parametrize("fault", ["receipt", "cancel", "replay", "settlement"])
async def test_start_handoff_failure_drains_only_new_owned_handle(
    make_process, tmp_path: Path, monkeypatch, fault: str
):
    case = make_process(python_code="import time; time.sleep(30)")
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            peer = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            peer_prepared = peer.prepare(case.workspace, (), budget=GitOperationBudget(20))
            prepared = (
                peer_prepared
                if fault == "replay"
                else case.port.prepare(case.workspace, (), budget=GitOperationBudget(20))
            )
            peer_plan, plan = original._plan(case, peer_prepared), original._plan(case, prepared)
            if fault == "replay":
                plan = peer_plan
            peer_ready = asyncio.Event()
            refresh = SupervisedProcess.refresh
            stop = SupervisedProcess.stop
            seen = []
            body_error = (
                asyncio.CancelledError()
                if fault == "cancel"
                else KernelError("process_owner_receipt_invalid", "测试启动握手故障")
            )
            cleanup_error = KernelError("process_control_lost", "测试停止结算故障")

            async def injected_refresh(handle):
                result = await refresh(handle)
                if result.state == "running":
                    if result.process_id == peer_prepared.spec.process_id:
                        peer_ready.set()
                    if (
                        fault != "replay"
                        and result.process_id == prepared.spec.process_id
                        and not seen
                    ):
                        seen.append(result.pid)
                        raise body_error
                return result

            async def injected_stop(handle, reason="cancelled"):
                if fault == "settlement" and handle.lease.process_id == prepared.spec.process_id:
                    raise cleanup_error
                return await stop(handle, reason)

            peer_cancel = CancelToken()
            monkeypatch.setattr(SupervisedProcess, "refresh", injected_refresh)
            monkeypatch.setattr(SupervisedProcess, "stop", injected_stop)
            task = asyncio.create_task(
                peer.run(
                    peer_prepared,
                    peer_plan,
                    peer_cancel,
                    budget=peer_prepared.budget,
                    checkpoint=original._checkpoint(peer_plan),
                )
            )
            try:
                async with asyncio.timeout(10):
                    await peer_ready.wait()
                expected = asyncio.CancelledError if fault == "cancel" else KernelError
                with pytest.raises(expected) as caught:
                    await case.port.run(
                        prepared,
                        plan,
                        CancelToken(),
                        budget=prepared.budget,
                        checkpoint=original._checkpoint(plan),
                    )
                if fault == "settlement":
                    assert caught.value.code == "git_process_unknown"
                    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
                    assert caught.value.__cause__.exceptions == (body_error, cleanup_error)
                elif fault == "replay":
                    assert caught.value.code == "process_already_exists"
                else:
                    assert len(seen) == 1
                    if fault != "cancel":
                        assert caught.value is body_error
                    assert not await asyncio.to_thread(original._process_running, seen[0])
                    assert supervisor.status(prepared.spec.process_id).stop_reason == "cancelled"
                peer_pid = supervisor.status(peer_prepared.spec.process_id).pid
                assert await asyncio.to_thread(original._process_running, peer_pid)
                assert not task.done() and not supervisor._closed and not plans._closed
                host.checkpoint(case.state)
            finally:
                monkeypatch.setattr(SupervisedProcess, "refresh", refresh)
                monkeypatch.setattr(SupervisedProcess, "stop", stop)
                if fault != "replay" and prepared.spec.process_id in supervisor._handles:
                    await supervisor._handles[prepared.spec.process_id].aclose()
                peer_cancel.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await peer.aclose()
    protection.close()


@pytest.mark.parametrize("cleanup", ["unknown", "raises"])
@pytest.mark.parametrize("outer_stop", ["none", "startup_cancel", "token", "task", "timeout"])
async def test_start_cleanup_final_lease_and_cause_survive_outer_stop(
    make_process, tmp_path: Path, monkeypatch, cleanup: str, outer_stop: str
):
    """真实Owner停止后删除自有终态回执，验证UNKNOWN与外层停止竞争的强失败。"""
    case = make_process(python_code="import time; time.sleep(30)")
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with (
        product_state_owner(case.state) as owner,
        SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
    ):
        async with supervisor_type(
            case.state / "process-owner", output_redaction=protection
        ) as supervisor:
            host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
            peer = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            case.port = GitDeliveryProcess(
                case.runner, case.state, output_redaction=protection, runtime_host=host
            )
            peer_prepared = peer.prepare(case.workspace, (), budget=GitOperationBudget(20))
            prepared = case.port.prepare(
                case.workspace, (), budget=GitOperationBudget(2 if outer_stop == "timeout" else 20)
            )
            peer_plan, plan = original._plan(case, peer_prepared), original._plan(case, prepared)
            peer_ready, cleanup_ready = asyncio.Event(), asyncio.Event()
            cleanup_release, draining = asyncio.Event(), asyncio.Event()
            refresh, stop = SupervisedProcess.refresh, SupervisedProcess.stop
            drain = delivery._drain
            seen = []
            body_error = (
                asyncio.CancelledError()
                if outer_stop == "startup_cancel"
                else KernelError("process_owner_receipt_invalid", "测试启动握手故障")
            )
            cleanup_error = KernelError("process_control_lost", "测试停止结算故障")

            async def injected_refresh(handle):
                result = await refresh(handle)
                if result.state == "running":
                    if result.process_id == peer_prepared.spec.process_id:
                        peer_ready.set()
                    if result.process_id == prepared.spec.process_id and not seen:
                        seen.append(result.pid)
                        raise body_error
                return result

            async def injected_stop(handle, reason="cancelled"):
                if handle.lease.process_id != prepared.spec.process_id:
                    return await stop(handle, reason)
                cleanup_ready.set()
                await cleanup_release.wait()
                await stop(handle, reason)
                # 原Owner确已停止后才删除本夹具自己的回执，原refresh自行持久化UNKNOWN。
                assert handle._owner is not None
                await asyncio.to_thread(handle._owner.wait, timeout=10)
                if cleanup == "unknown":
                    (handle._run_directory / "receipt.json").unlink()
                else:
                    raise cleanup_error

            async def observe_drain(task):
                if case.port._active is not None and task is case.port._active[1]:
                    draining.set()
                return await drain(task)

            peer_cancel, target_cancel = CancelToken(), CancelToken()
            monkeypatch.setattr(SupervisedProcess, "refresh", injected_refresh)
            monkeypatch.setattr(SupervisedProcess, "stop", injected_stop)
            monkeypatch.setattr(delivery, "_drain", observe_drain)
            peer_task = asyncio.create_task(
                peer.run(
                    peer_prepared,
                    peer_plan,
                    peer_cancel,
                    budget=peer_prepared.budget,
                    checkpoint=original._checkpoint(peer_plan),
                )
            )
            target_task = None
            try:
                async with asyncio.timeout(10):
                    await peer_ready.wait()
                target_task = asyncio.create_task(
                    case.port.run(
                        prepared,
                        plan,
                        target_cancel,
                        budget=prepared.budget,
                        checkpoint=original._checkpoint(plan),
                    )
                )
                async with asyncio.timeout(10):
                    await cleanup_ready.wait()
                    if outer_stop == "token":
                        target_cancel.cancel()
                    elif outer_stop == "task":
                        target_task.cancel()
                    if outer_stop in {"token", "task", "timeout"}:
                        await draining.wait()
                    cleanup_release.set()
                    with pytest.raises(KernelError) as caught:
                        await target_task
                assert caught.value.code == "git_process_unknown"
                assert isinstance(caught.value.__cause__, BaseExceptionGroup)
                causes = caught.value.__cause__.exceptions
                assert len(causes) == 2 and causes[0] is body_error
                if cleanup == "unknown":
                    assert isinstance(causes[1], KernelError)
                    assert causes[1].code == "git_process_unknown"
                    assert supervisor.status(prepared.spec.process_id).state == "unknown"
                else:
                    assert causes[1] is cleanup_error
                assert not await asyncio.to_thread(original._process_running, seen[0])
                peer_pid = supervisor.status(peer_prepared.spec.process_id).pid
                assert await asyncio.to_thread(original._process_running, peer_pid)
                assert not peer_task.done() and not supervisor._closed and not plans._closed
                host.checkpoint(case.state)
            finally:
                cleanup_release.set()
                target_cancel.cancel()
                if target_task is not None:
                    await asyncio.gather(target_task, return_exceptions=True)
                monkeypatch.setattr(SupervisedProcess, "refresh", refresh)
                monkeypatch.setattr(SupervisedProcess, "stop", stop)
                monkeypatch.setattr(delivery, "_drain", drain)
                if prepared.spec.process_id in supervisor._handles:
                    await supervisor._handles[prepared.spec.process_id].aclose()
                peer_cancel.cancel()
                await asyncio.gather(peer_task, return_exceptions=True)
                await case.port.aclose()
                await peer.aclose()
    protection.close()
