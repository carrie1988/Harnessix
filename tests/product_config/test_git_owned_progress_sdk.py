"""实际认证 SDK Patch 链：构造观察异常不能冒充归属拒绝。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_delivery_source
from harnessix.product_config.git_delivery_source import verify_git_delivery_source
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.support.git_user_observation import run_authenticated_observation


@pytest.mark.parametrize("store_kind", ["audit", "transactions"])
@pytest.mark.parametrize(
    "code",
    [
        "workspace_patch_source_not_owned",
        "workspace_patch_source_not_published",
        "delivery_blob_corrupt",
        "delivery_blob_invalid",
    ],
)
@pytest.mark.parametrize("layered", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_original_store_observer_same_ownership_code_preserves_first_object(
    tmp_path, config, monkeypatch, store_kind, code, layered, nested
):
    async def inspect(scenario):
        source = (await scenario.collect()).baseline.source
        before = scenario.unchanged_state()
        error = KernelError(code, "original observer, not Patch ownership data")
        if nested:
            error = UpstreamCheckpointError(UpstreamCheckpointError(error))
        observed = []

        def observer():
            observed.append(store_kind)
            raise error

        owner = scenario.router._audit if store_kind == "audit" else scenario.transactions
        check = CancelToken().checkpoint
        control = GitAuthenticationControl(check, check) if layered else check
        with monkeypatch.context() as patch:
            patch.setattr(owner, "_checkpoint", observer)
            with pytest.raises(BaseException) as caught:
                verify_git_delivery_source(
                    scenario.thread,
                    source,
                    scenario.router,
                    scenario.transactions,
                    checkpoint=control,
                    snapshot_ports=scenario.router._snapshot_ports,
                )
        assert caught.value is error and observed == [store_kind]
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)


@pytest.mark.parametrize("store_kind", ["audit", "transactions"])
@pytest.mark.parametrize("phase", ["first_local", "second_patch_local"])
@pytest.mark.parametrize("nested", [False, True])
async def test_original_observer_late_local_failure_keeps_object_and_state(
    tmp_path, config, monkeypatch, store_kind, phase, nested
):
    async def inspect(scenario):
        source = (await scenario.collect()).baseline.source
        before = scenario.unchanged_state()
        error = KernelError("workspace_patch_source_not_owned", "late original observer")
        if nested:
            error = UpstreamCheckpointError(UpstreamCheckpointError(error))
        owner = scenario.router._audit if store_kind == "audit" else scenario.transactions
        original_observer = owner._checkpoint
        original_factory = git_delivery_source.same_task_io_git_authentication
        active, segment, local_calls, failed_at = False, 0, 0, []

        @contextmanager
        def factory(control):
            nonlocal active, segment
            with original_factory(control) as progress:
                segment += 1
                active = True
                try:
                    yield progress
                finally:
                    active = False

        def observer():
            nonlocal local_calls
            if original_observer is not None:
                original_observer()
            if active:
                local_calls += 1
                # 每个 Patch 原 Audit 与 Transaction 各有两个计算段；第五段已进入第二个。
                if (phase == "first_local" or segment >= 5) and local_calls >= 2:
                    failed_at.append((segment, local_calls))
                    raise error

        check = CancelToken().checkpoint
        control = GitAuthenticationControl(check, check)
        with monkeypatch.context() as patch:
            patch.setattr(owner, "_checkpoint", observer)
            patch.setattr(git_delivery_source, "same_task_io_git_authentication", factory)
            with pytest.raises(BaseException) as caught:
                verify_git_delivery_source(
                    scenario.thread,
                    source,
                    scenario.router,
                    scenario.transactions,
                    checkpoint=control,
                    snapshot_ports=scenario.router._snapshot_ports,
                )
        assert caught.value is error and len(failed_at) == 1
        assert failed_at[0][0] >= (5 if phase == "second_patch_local" else 1)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)


async def test_complete_authenticated_continuous_source_keeps_original_reads_and_effects(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        source = (await scenario.collect()).baseline.source
        before = scenario.unchanged_state()
        original = scenario.transactions._read_blob
        reads, local, full = [], [], []
        cancel = CancelToken()

        def read(digest):
            reads.append(digest)
            return original(digest)

        def checkpoint():
            cancel.checkpoint()
            full.append(1)

        def local_check():
            cancel.checkpoint()
            local.append(1)

        monkeypatch.setattr(scenario.transactions, "_read_blob", read)
        control = GitAuthenticationControl(local_check, checkpoint)
        verify_git_delivery_source(
            scenario.thread,
            source,
            scenario.router,
            scenario.transactions,
            checkpoint=checkpoint,
            snapshot_ports=scenario.router._snapshot_ports,
        )
        old_reads = tuple(reads)
        reads.clear()
        verify_git_delivery_source(
            scenario.thread,
            source,
            scenario.router,
            scenario.transactions,
            checkpoint=control,
            snapshot_ports=scenario.router._snapshot_ports,
        )
        assert tuple(reads) == old_reads and local and full
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)


@pytest.mark.parametrize("code", ["delivery_blob_corrupt", "delivery_blob_invalid"])
@pytest.mark.parametrize("edge", ["before", "after"])
@pytest.mark.parametrize("layered", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_final_snapshot_last_blob_observer_is_not_historical_corruption(
    tmp_path, config, monkeypatch, code, edge, layered, nested
):
    async def inspect(scenario):
        source = (await scenario.collect()).baseline.source
        before = scenario.unchanged_state()
        original = scenario.transactions._read_blob
        reads = []

        def read(digest):
            reads.append(digest)
            return original(digest)

        check = CancelToken().checkpoint
        control = GitAuthenticationControl(check, check) if layered else check
        root, ports = Path(scenario.thread.workspace), scenario.router._snapshot_ports
        with monkeypatch.context() as patch:
            patch.setattr(scenario.transactions, "_read_blob", read)
            git_delivery_source._verify_final_snapshot(source.workspace, root, control, ports)
            expected_reads = tuple(reads)
            assert len(expected_reads) >= 2
            reads.clear()
            error = KernelError(code, "final Snapshot original observer, not physical CAS")
            if nested:
                error = UpstreamCheckpointError(UpstreamCheckpointError(error))
            fail_at = 2 * (len(expected_reads) - 1) + (1 if edge == "before" else 2)
            observations = []

            def observer():
                observations.append(len(reads))
                if len(observations) == fail_at:
                    raise error

            patch.setattr(scenario.transactions, "_checkpoint", observer)
            with pytest.raises(BaseException) as caught:
                git_delivery_source._verify_final_snapshot(source.workspace, root, control, ports)
        assert caught.value is error
        assert tuple(reads) == expected_reads[: len(expected_reads) - (edge == "before")]
        assert len(observations) == fail_at
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)
