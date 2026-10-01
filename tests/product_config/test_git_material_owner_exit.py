"""原 Owner 控制失联及完整退出回归；不以合成句柄替代真实进程生命周期。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.processes.owner_receipt import verify_owner_receipt
from harnessix.processes.supervisor import PosixProcessSupervisor, SupervisedProcess
from harnessix.product_config import git_material_process as material_process
from harnessix.product_config.git_delivery_process import GitOperationBudget
from tests.product_config import test_git_delivery_process as process_tests
from tests.product_config import test_git_material_input as input_tests

make_process = input_tests.make_process


async def _settle_actual_owner(case, prepared, captured) -> None:
    # 原 aclose 的失联异常不伪造终态；测试独立排空同一真实句柄并验真。
    handle = captured["handle"]
    try:
        lease = await asyncio.wait_for(handle.wait(), 10)
        assert lease.state == "exited" and lease.pid is not None
        await process_tests._wait_stopped((lease.pid,))
        receipt = process_tests._receipt(case, prepared)
        assert (
            verify_owner_receipt(
                receipt,
                owner_token=lease.owner_token,
                process_id=lease.process_id,
                owner_identity=lease.owner_identity,
            )
            == receipt
        )
        assert receipt.pid == lease.pid and receipt.raw_stdout.eof and receipt.raw_stderr.eof
        for stream in ("stdout", "stderr"):
            body = await handle.output(stream)
            observed = getattr(receipt, f"raw_{stream}")
            assert observed.observed_bytes == len(body)
            assert observed.sha256 == hashlib.sha256(body).hexdigest()
    finally:
        captured["supervisor"]._store.close()


@pytest.mark.parametrize("mode", ["operation-error", "caller-cancel", "deadline"])
async def test_real_control_loss_preserves_unknown_after_full_supervisor_exit(
    make_process, tmp_path, monkeypatch, mode
):
    case = make_process(output_redaction=input_tests._Protection())
    binding = input_tests._repository(case, tmp_path, "sha1")
    budget = GitOperationBudget(2 if mode == "deadline" else 45)
    prepared = input_tests._prepare(
        case, binding, input_tests._material(b"owner-exit-material", "sha1", "blob"), budget
    )
    plan, cancel = process_tests._plan(case, prepared), CancelToken()
    entered, release = asyncio.Event(), asyncio.Event()
    captured = {}
    sent, exit_errors = [], []
    original_start = PosixProcessSupervisor.start
    original_close_stdin = SupervisedProcess.close_stdin
    original_write = SupervisedProcess._write_all
    original_close = PosixProcessSupervisor.aclose

    async def capture_start(supervisor, *args, **kwargs):
        handle = await original_start(supervisor, *args, **kwargs)
        captured.update(supervisor=supervisor, handle=handle)
        return handle

    async def hold_close_stdin(handle):
        # manifest 已实际写入原控制通道；先阻塞 EOF，再注入控制写 OSError。
        assert sent == [prepared.write.control_input]
        entered.set()
        await release.wait()
        await original_close_stdin(handle)

    def fail_eof_write(descriptor, body):
        command = json.loads(body)
        if command.get("operation") == "close_stdin":
            raise OSError("injected original control write failure")
        original_write(descriptor, body)
        if command.get("operation") == "stdin":
            sent.append(base64.b64decode(command["data_base64"]))

    async def observe_close(supervisor):
        try:
            await original_close(supervisor)
        except KernelError as error:
            exit_errors.append(error.code)
            raise

    monkeypatch.setattr(PosixProcessSupervisor, "start", capture_start)
    monkeypatch.setattr(SupervisedProcess, "close_stdin", hold_close_stdin)
    monkeypatch.setattr(SupervisedProcess, "_write_all", staticmethod(fail_eof_write))
    monkeypatch.setattr(PosixProcessSupervisor, "aclose", observe_close)
    task = asyncio.create_task(
        case.port.run(
            prepared, plan, cancel, budget=budget, checkpoint=process_tests._checkpoint(plan)
        )
    )
    try:
        barrier = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                (task, barrier), timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
            assert barrier in done, "原控制通道 EOF 屏障未到达"
        finally:
            barrier.cancel()
            await asyncio.gather(barrier, return_exceptions=True)
        handle = captured["handle"]
        pid = handle.lease.pid
        assert pid is not None and await asyncio.to_thread(process_tests._process_running, pid)
        if mode == "caller-cancel":
            cancel.cancel()
        elif mode == "deadline":
            # 等待的是原绝对预算，既不重置期限，也不猜测 Owner 的执行速度。
            await asyncio.sleep(budget.remaining() + 0.1)
        release.set()
        with pytest.raises(KernelError) as failure:
            await task
        assert failure.value.code == "git_material_effect_unknown"
        assert exit_errors == ["process_not_owned"]
        assert captured["supervisor"]._closed and handle._closed
        assert handle._control_fd is None and case.port._active is None
        assert not await asyncio.to_thread(Path(prepared.write.request.body_path).exists)
    finally:
        release.set()
        cancel.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if "handle" in captured:
            await _settle_actual_owner(case, prepared, captured)


@pytest.mark.parametrize("delivered", [False, True], ids=["before-manifest", "after-proof"])
async def test_real_foreign_stage_identity_is_not_deleted_or_reported_success(
    make_process, tmp_path, monkeypatch, delivered
):
    case = make_process(output_redaction=input_tests._Protection())
    binding = input_tests._repository(case, tmp_path, "sha1")
    prepared = input_tests._prepare(
        case, binding, input_tests._material(b"stage-identity-material", "sha1", "blob")
    )
    plan, cancel = process_tests._plan(case, prepared), CancelToken()
    original_stage = material_process.stage_material
    original_remove = material_process.StagedGitMaterial.remove
    stage = Path(prepared.write.request.body_path)
    preserved = stage.with_name("preserved-owned-stage.bin")

    def stage_then_cancel(write, operation, budget):
        result = original_stage(write, operation, budget)
        if not delivered:
            operation.cancel()
        return result

    def replace_stage_at_cleanup(staged):
        # 实际更换 inode，而不是合成 remove 异常；旧文件与陌生文件均不得误删。
        staged.path.rename(preserved)
        staged.path.write_bytes(b"foreign-stage")
        original_remove(staged)

    monkeypatch.setattr(material_process, "stage_material", stage_then_cancel)
    monkeypatch.setattr(material_process.StagedGitMaterial, "remove", replace_stage_at_cleanup)
    try:
        with pytest.raises(KernelError) as failure:
            await case.port.run(
                prepared,
                plan,
                cancel,
                budget=prepared.budget,
                checkpoint=process_tests._checkpoint(plan),
            )
        assert failure.value.code == (
            "git_material_effect_unknown" if delivered else "git_material_stage_changed"
        )
        assert await asyncio.to_thread(stage.read_bytes) == b"foreign-stage"
        assert await asyncio.to_thread(preserved.read_bytes) == prepared.write.material.body
        lease = process_tests._lease(case, prepared)
        assert lease.state == "exited" and lease.pid is not None
        await process_tests._wait_stopped((lease.pid,))
        receipt = process_tests._receipt(case, prepared)
        assert receipt.raw_stdout.eof and receipt.raw_stderr.eof
        if delivered:
            read = case.port.prepare_object_read(
                case.workspace,
                GitObjectRead("blob", prepared.write.material.object_id, "sha1"),
                budget=prepared.budget,
            )
            assert (await process_tests._run(case, read)).material == prepared.write.material
        else:
            object_path = (
                case.workspace
                / ".git/objects"
                / prepared.write.material.object_id[:2]
                / prepared.write.material.object_id[2:]
            )
            assert not await asyncio.to_thread(object_path.exists)
    finally:
        await asyncio.to_thread(stage.unlink, missing_ok=True)
        await asyncio.to_thread(preserved.unlink, missing_ok=True)
