"""独立可选预测试候选不改旧配置、模型、任务或费用边界。"""

import pytest

from harnessix.evals.swebench.run_config import (
    SweBenchOptionalPreEditAgentRunConfig,
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
        allow_optional_pre_edit_profile=True,
    )
    settings.update(changes)
    return build_swebench_agent_run_config(**settings)


def test_optional_pre_edit_factory_roundtrip_without_extra_task_or_fee_budget(tmp_path):
    config = factory(tmp_path)
    legacy = factory(tmp_path, allow_optional_pre_edit_profile=False)
    assert type(config) is SweBenchOptionalPreEditAgentRunConfig
    assert config.optional_pre_edit_profile_enabled and config.budget_pressure_compaction
    assert not config.planning_enabled and not config.thinking_enabled
    assert config.model_dump(exclude={"spec_version", "pre_edit_profile_policy"}) == (
        legacy.model_dump(exclude={"spec_version"})
    )
    assert config.fingerprint != legacy.fingerprint
    assert config.worst_case_amount(build_agent_test_plan()) == legacy.worst_case_amount(
        build_agent_test_plan()
    )
    config.require_plan(build_agent_test_plan())
    path = tmp_path / "optional.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_optional_pre_edit_profile": None},
        {"allow_optional_pre_edit_profile": 1},
        {"allow_optional_pre_edit_profile": "true"},
        {"enable_planning": True},
        {"enable_thinking": True},
        {"enable_grep_snapshots": True},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
    ],
)
def test_optional_pre_edit_factory_rejects_implicit_or_combined_modes(tmp_path, changes):
    with pytest.raises(ValueError):
        factory(tmp_path, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"pre_edit_profile_policy": "tampered"},
        {"optional_pre_edit_profile_enabled": True},
        {"extra_field": True},
    ],
)
def test_optional_config_rejects_unknown_or_tampered_fields(tmp_path, changes):
    config = factory(tmp_path)
    with pytest.raises(ValueError):
        type(config).model_validate({**config.model_dump(), **changes})
