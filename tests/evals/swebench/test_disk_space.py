"""余量不足时零克隆、零付费请求、零官方构建；只读验收不受影响。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench import disk_space, execution, official
from tests.evals.swebench.test_execution import NOW, _config, _offline_suite, _plan, _turn


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_exact_disk_floor(monkeypatch, tmp_path, delta):
    free = disk_space.MIN_EVALUATION_FREE_BYTES + delta
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    if delta < 0:
        with pytest.raises(KernelError) as failure:
            disk_space.require_evaluation_disk_space(tmp_path)
        assert failure.value.code == "swebench_disk_space_low"
    else:
        disk_space.require_evaluation_disk_space(tmp_path)


def test_new_directory_checks_existing_parent_without_creating_it(monkeypatch, tmp_path):
    calls = []

    def usage(path):
        calls.append(path)
        if not path.exists():
            raise FileNotFoundError
        return SimpleNamespace(free=disk_space.MIN_EVALUATION_FREE_BYTES)

    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    new = tmp_path / "not-created" / "run"
    disk_space.require_evaluation_disk_space(new)
    assert calls == [new, new.parent, tmp_path]
    assert not new.parent.exists()


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unknown_disk_state_fails_closed(monkeypatch, tmp_path, error):
    monkeypatch.setattr(disk_space.shutil, "disk_usage", Mock(side_effect=error))
    with pytest.raises(KernelError) as failure:
        disk_space.require_evaluation_disk_space(tmp_path)
    assert failure.value.code == "swebench_disk_state_unavailable"


def test_each_filesystem_must_have_headroom(monkeypatch, tmp_path):
    usage = Mock(side_effect=[SimpleNamespace(free=20 * 1024**3), SimpleNamespace(free=1)])
    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    with pytest.raises(KernelError) as failure:
        disk_space.require_evaluation_disk_space(tmp_path, tmp_path.parent)
    assert failure.value.code == "swebench_disk_space_low"
    assert usage.call_count == 2


async def test_low_space_stops_before_workspace_or_provider(monkeypatch, tmp_path):
    plan = _plan()
    config = _config(tmp_path, plan)
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    materialize, provider = AsyncMock(), AsyncMock()
    monkeypatch.setattr(execution, "_materialize_all", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", provider)
    with pytest.raises(KernelError) as failure:
        await execution.run_swebench_agent_suite(
            plan,
            config,
            api_key="offline-fixture",
            allow_repository_network=False,
            allow_provider_network=True,
        )
    assert failure.value.code == "swebench_disk_space_low"
    materialize.assert_not_called()
    provider.assert_not_called()
    assert not (Path(config.work_root) / "cases").exists()


def test_low_space_never_starts_official_evaluator(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    run = Mock(side_effect=AssertionError("must not build"))
    monkeypatch.setattr(official.subprocess, "run", run)
    with pytest.raises(KernelError) as failure:
        official.run_official_evaluation(
            command=("never-run",),
            work_root=tmp_path,
            allow_network=True,
        )
    assert failure.value.code == "swebench_disk_space_low"
    run.assert_not_called()


async def test_disk_drift_stops_next_case_and_preserves_completed_state(monkeypatch, tmp_path):
    plan = _plan()
    config = _config(tmp_path, plan)
    calls = _offline_suite(monkeypatch, plan, config, [_turn(i) for i in range(25)])
    monkeypatch.setattr(execution, "utc_now", lambda: NOW)
    monkeypatch.setattr(execution, "_turn_cost", lambda *_: (0, True))

    def gate(*_paths):
        if calls:
            raise KernelError("swebench_disk_space_low", "low disk")

    monkeypatch.setattr(execution, "require_evaluation_disk_space", gate)
    with pytest.raises(KernelError) as failure:
        await execution.run_swebench_agent_suite(
            plan,
            config,
            api_key="offline-fixture",
            allow_repository_network=False,
            allow_provider_network=True,
        )
    assert failure.value.code == "swebench_disk_space_low"
    assert calls == [plan.cases[0].instance_id]
    root = Path(config.work_root)
    state = execution.read_suite_execution_state(root / "suite-state.json", plan)
    assert state.case_states[0].status == "completed"
    assert state.case_states[1].status == "ready"
