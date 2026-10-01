"""完整材料快照的真实进程死亡屏障；不冒充默认写 Tool 或 Git 业务成功。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_input_contracts import encode_manifest
from harnessix.processes.owner_receipt import verify_owner_receipt
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_material_process import stage_material
from tests.product_config import test_git_delivery_process as process_tests
from tests.product_config import test_git_material_input as input_tests

make_process = process_tests.make_process


def _program(marker: Path) -> str:
    import harnessix

    # 在新基础解释器中调用实际 snapshot，并在第一次真实写入后阻塞。
    # 仅注入故障屏障，不执行 Git、不构造材料成功证明、不依赖时间猜测。
    root = Path(harnessix.__file__).resolve().parent.parent
    return f"""
import os,sys,time
from pathlib import Path
sys.path.insert(0,{str(root)!r})
from harnessix.delivery.git_material_input_contracts import decode_manifest,MAX_MANIFEST_BYTES
from harnessix.delivery import git_material_native as native
from harnessix.delivery.git_material_native_windows import _Windows
request=decode_manifest(sys.stdin.buffer.read(MAX_MANIFEST_BYTES+1))
original=native.os.write
def held(fd,body):
    count=original(fd,body)
    Path({str(marker)!r}).write_bytes(b'snapshot-first-write')
    while True: time.sleep(1)
native.os.write=held
with native._Resources() as resources:
    native._snapshot(request,resources,_Windows() if os.name=='nt' else None)
raise AssertionError('failure barrier did not hold')
"""


async def _wait_marker(task, marker: Path) -> None:
    deadline = time.monotonic() + 10
    while not await asyncio.to_thread(marker.exists):
        if task.done():
            await task
            raise AssertionError("快照故障屏障前根进程已终结")
        if time.monotonic() >= deadline:
            raise AssertionError("快照故障屏障未确认实际首次写入")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["cancel", "hard-kill", "timeout"])
async def test_actual_owner_reaps_snapshot_without_named_partial_material(
    make_process,
    tmp_path,
    mode,
):
    source = make_process(output_redaction=input_tests._Protection())
    binding = input_tests._repository(source, tmp_path, "sha1")
    material = input_tests._material(b"x" * input_tests._LIMIT, "sha1", "blob")
    prepared_source = input_tests._prepare(source, binding, material)
    # 合成材料是测试布置，不通过此调用宣称产品完成了批准前置保护。
    staged = stage_material(prepared_source.write, CancelToken(), prepared_source.budget)
    marker = tmp_path / "snapshot-first-write.ready"
    root = make_process(python_code=_program(marker), output_redaction=input_tests._Protection())
    budget = GitOperationBudget(2 if mode == "timeout" else 30)
    prepared = root.port.prepare(
        root.workspace,
        ("synthetic-snapshot-barrier",),
        budget=budget,
        input_data=encode_manifest(prepared_source.write.request),
    )
    cancel = CancelToken()
    plan = process_tests._plan(root, prepared)
    task = asyncio.create_task(
        root.port.run(
            prepared,
            plan,
            cancel,
            budget=budget,
            checkpoint=process_tests._checkpoint(plan),
        )
    )
    try:
        await _wait_marker(task, marker)
        from harnessix.processes.supervision_store import SQLiteProcessLeaseStore

        with SQLiteProcessLeaseStore(
            root.state / "process-owner/process-leases.db", read_only=True
        ) as store:
            pid = store.load(prepared.spec.process_id).pid
        assert pid is not None and await asyncio.to_thread(process_tests._process_running, pid)
        if mode == "cancel":
            cancel.cancel()
        elif mode == "hard-kill":
            await asyncio.to_thread(process_tests._emergency_stop, pid)
        expected = TurnCancelled if mode == "cancel" else KernelError
        with pytest.raises(expected):
            await task
        lease = process_tests._lease(root, prepared)
        assert lease.pid == pid and lease.state != "unknown"
        await process_tests._wait_stopped((pid,))
        receipt = process_tests._receipt(root, prepared)
        assert (
            verify_owner_receipt(
                receipt,
                owner_token=lease.owner_token,
                process_id=lease.process_id,
                owner_identity=lease.owner_identity,
            )
            == receipt
        )
        assert receipt.raw_stdout.eof and receipt.raw_stderr.eof
        assert not await asyncio.to_thread(
            lambda: tuple(Path(prepared_source.write.request.stage_root).glob("snapshot-*.bin"))
        )
    finally:
        cancel.cancel()
        await root.port.aclose()
        if not task.done():
            await task
        staged.remove()
