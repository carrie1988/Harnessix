"""工作集视图独立冻结为v13；任务、模型、费用边界不变，不与其他候选隐式组合。"""

import json

import pytest
from pydantic import ValidationError

from harnessix.evals.swebench.run_config import (
    SweBenchRecyclingAgentRunConfig,
    SweBenchWorkingSetAgentRunConfig,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from tests.evals.swebench.test_optional_pre_edit_run_config import factory
from tests.evals.swebench.test_run_config import build_agent_test_plan


def working_set_config(tmp_path, **changes):
    flags = dict(
        allow_optional_pre_edit_profile=False,
        recycle_workspaces=True,
        working_set_result_groups=2,
    )
    flags.update(changes)
    return factory(tmp_path, **flags)


def test_working_set_v13_roundtrips_without_changing_task_model_or_fee_budget(tmp_path):
    config = working_set_config(tmp_path)
    baseline = working_set_config(tmp_path, working_set_result_groups=None)
    assert type(config) is SweBenchWorkingSetAgentRunConfig
    assert type(baseline) is SweBenchRecyclingAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v13"
    assert config.working_set_result_groups == 2 and baseline.working_set_result_groups is None
    assert config.isolate_provider_failures and config.budget_pressure_compaction
    assert config.workspace_retirement_enabled
    assert not config.definition_views_enabled and not config.regex_search_enabled
    assert not config.closing_phase_enabled and not config.grep_snapshots_enabled
    assert not config.thinking_enabled and not config.planning_enabled
    assert config.provider_config == baseline.provider_config
    assert config.price == baseline.price and config.billing_context == baseline.billing_context
    assert config.fee_stop_amount == baseline.fee_stop_amount
    assert config.case_process_profiles == baseline.case_process_profiles
    plan = build_agent_test_plan()
    assert config.worst_case_amount(plan) == baseline.worst_case_amount(plan)
    assert config.fingerprint != baseline.fingerprint
    config.require_plan(plan)
    path = tmp_path / "working-set.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert type(restored) is SweBenchWorkingSetAgentRunConfig
    assert restored == config and restored.fingerprint == config.fingerprint
    assert json.loads(path.read_text())["retain_recent_result_groups"] == 2


def test_retained_group_count_is_part_of_the_fingerprint(tmp_path):
    one = working_set_config(tmp_path, working_set_result_groups=1)
    two = working_set_config(tmp_path, working_set_result_groups=2)
    assert one.fingerprint != two.fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"working_set_result_groups": 0},
        {"working_set_result_groups": 65},
        {"working_set_result_groups": True},
        {"working_set_result_groups": "2"},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
        {"recycle_workspaces": False},
        {"enable_definition_views": True},
        {"enable_regex_search": True},
        {"reserve_closing_phase": True},
        {"enable_grep_snapshots": True},
        {"allow_optional_pre_edit_profile": True},
        {"enable_planning": True},
        {"enable_thinking": True},
    ],
)
def test_working_set_factory_rejects_invalid_counts_missing_guards_and_mixed_candidates(
    tmp_path, changes
):
    with pytest.raises((ValueError, ValidationError)):
        working_set_config(tmp_path, **changes)
