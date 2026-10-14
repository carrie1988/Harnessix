"""Suite取消必须等待不可强杀的回收线程闭合，不能提前释放单写者锁。"""

import asyncio
import threading
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.execution_fs import exclusive_execution_lock
from harnessix.evals.swebench import execution
from harnessix.evals.swebench.run_config import SweBenchRecyclingAgentRunConfig
from tests.evals.swebench.test_execution import NOW, PATCH, _config, _plan, _turn


def _suite_lock(root):
    return exclusive_execution_lock(
        root,
        execution._LOCK_FILE,
        busy_code="busy",
        invalid_code="invalid",
        label="test-suite",
    )


@pytest.mark.parametrize("repeated_cancel", [False, True])
async def test_parent_cancel_waits_for_retirement_before_releasing_suite_lock(
    tmp_path, monkeypatch, repeated_cancel
):
    plan = _plan()
    config = SweBenchRecyclingAgentRunConfig.model_validate(
        {
            **_config(tmp_path, plan).model_dump(),
            "spec_version": "harnessix.swebench-agent-run-config/v9",
        }
    )
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    cancel = CancelToken()
    cancel_acknowledged = asyncio.Event()
    signal_cancel = cancel.cancel
    turns = []
    observed_cancel = []

    def acknowledge_cancel():
        signal_cancel()
        cancel_acknowledged.set()

    def materialize(case, **_kwargs):
        root = Path(config.work_root) / "workspaces" / case.instance_id
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root

    async def open_turn(*_args):
        turns.append(1)
        return UUID(int=500), _turn(0)

    def blocked_retirement(*_args, cancel, **_kwargs):
        entered.set()
        try:
            if not release.wait(5):
                raise AssertionError("test did not release worker")
            observed_cancel.append(cancel.cancelled)
            cancel.checkpoint()
            pytest.fail("父Task取消后不得继续删除")
        finally:
            closed.set()

    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)
    monkeypatch.setattr(execution, "utc_now", lambda: NOW)
    monkeypatch.setattr(cancel, "cancel", acknowledge_cancel)
    monkeypatch.setattr(execution, "require_evaluation_disk_space", lambda *_: None)
    monkeypatch.setattr(execution, "materialize_swebench_workspace", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", open_turn)
    monkeypatch.setattr(execution, "_turn_cost", lambda *_: (0, True))
    monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: PATCH)
    monkeypatch.setattr(execution, "seal_and_retire_workspace", blocked_retirement)
    task = asyncio.create_task(
        execution.run_swebench_agent_suite(
            plan,
            config,
            api_key="offline-fixture",
            allow_repository_network=False,
            allow_provider_network=True,
            cancel=cancel,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        async with asyncio.timeout(2):
            await cancel_acknowledged.wait()
        assert not task.done() and not closed.is_set()
        with pytest.raises(KernelError) as error, _suite_lock(Path(config.work_root)):
            pytest.fail("后台清理未结算时Suite锁不得释放")
        assert error.value.code == "busy"
        if repeated_cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set() and observed_cancel == [True] and len(turns) == 1
    with _suite_lock(Path(config.work_root)):
        pass
