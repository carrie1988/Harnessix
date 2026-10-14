"""独立搜索快照候选不改旧配置、模型、任务或费用边界。"""

import pytest

from harnessix.evals.swebench.run_config import (
    SweBenchSearchSnapshotAgentRunConfig,
    build_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from tests.evals.swebench.test_run_config import _config, build_agent_test_plan


def factory(tmp_path, **changes):
    base = _config(tmp_path)
    settings = dict(
        plan=build_agent_test_plan(),
        source_root=tmp_path / "source",
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
        git_executable=tmp_path / "git",
        case_process_profiles=base.case_process_profiles,
        fee_stop_amount=base.fee_stop_amount,
        observed_at=base.price.valid_from,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_grep_snapshots=True,
    )
    settings.update(changes)
    return build_swebench_agent_run_config(**settings)


def test_search_snapshot_factory_roundtrip_without_extra_task_or_fee_budget(tmp_path):
    config = factory(tmp_path)
    legacy = factory(tmp_path, enable_grep_snapshots=False)
    assert type(config) is SweBenchSearchSnapshotAgentRunConfig
    assert config.grep_snapshots_enabled and config.budget_pressure_compaction
    assert not config.planning_enabled and not config.thinking_enabled
    assert config.model_dump(exclude={"spec_version", "search_snapshot_policy"}) == (
        legacy.model_dump(exclude={"spec_version"})
    )
    assert config.fingerprint != legacy.fingerprint
    assert config.worst_case_amount(build_agent_test_plan()) == legacy.worst_case_amount(
        build_agent_test_plan()
    )
    config.require_plan(build_agent_test_plan())
    path = tmp_path / "snapshot.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_grep_snapshots": None},
        {"enable_grep_snapshots": 1},
        {"enable_grep_snapshots": "true"},
        {"enable_planning": True},
        {"enable_thinking": True},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
    ],
)
def test_search_snapshot_factory_rejects_implicit_or_combined_modes(tmp_path, changes):
    with pytest.raises(ValueError):
        factory(tmp_path, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"search_snapshot_policy": "tampered"},
        {"grep_snapshots_enabled": True},
        {"extra_field": True},
    ],
)
def test_snapshot_config_rejects_unknown_or_tampered_fields(tmp_path, changes):
    config = factory(tmp_path)
    with pytest.raises(ValueError):
        type(config).model_validate({**config.model_dump(), **changes})
