"""回收策略显式冻结且不改变模型、预算、旧版本或单因素诊断。"""

import pytest

from harnessix.evals.swebench.run_config import SweBenchRecyclingAgentRunConfig
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from tests.evals.swebench.test_optional_pre_edit_run_config import factory


@pytest.mark.parametrize("strategy", ["standard", "search_snapshot", "optional_pre_edit"])
def test_recycling_config_freezes_strategy_and_preserves_task_and_fee_boundaries(
    tmp_path, strategy
):
    config = factory(
        tmp_path,
        recycle_workspaces=True,
        allow_optional_pre_edit_profile=strategy == "optional_pre_edit",
        enable_grep_snapshots=strategy == "search_snapshot",
    )
    assert type(config) is SweBenchRecyclingAgentRunConfig
    assert config.workspace_retirement_enabled and config.agent_strategy == strategy
    assert config.grep_snapshots_enabled is (strategy == "search_snapshot")
    assert config.optional_pre_edit_profile_enabled is (strategy == "optional_pre_edit")
    original = factory(
        tmp_path,
        recycle_workspaces=False,
        allow_optional_pre_edit_profile=strategy == "optional_pre_edit",
        enable_grep_snapshots=strategy == "search_snapshot",
    )
    assert config.provider_config == original.provider_config
    assert config.price == original.price and config.fee_stop_amount == original.fee_stop_amount
    assert config.case_process_profiles == original.case_process_profiles
    assert not original.workspace_retirement_enabled
    path = tmp_path / "recycling.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize("invalid", [None, "true", 1])
def test_recycling_requires_explicit_bool(tmp_path, invalid):
    with pytest.raises(ValueError):
        factory(tmp_path, recycle_workspaces=invalid)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_policy", "automatic_delete"),
        ("agent_strategy", "case_specific"),
        ("workspace_retirement_enabled", True),
    ],
)
def test_recycling_rejects_unregistered_policy_fields(tmp_path, field, value):
    config = factory(tmp_path, recycle_workspaces=True)
    with pytest.raises(ValueError):
        type(config).model_validate({**config.model_dump(), field: value})
