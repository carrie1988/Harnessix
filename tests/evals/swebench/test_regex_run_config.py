"""独立正则搜索版本维持原任务、模型、费用及认证回收边界。"""

import pytest

from harnessix.evals.swebench.run_config import SweBenchRegexAgentRunConfig
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from tests.evals.swebench.test_optional_pre_edit_run_config import factory
from tests.evals.swebench.test_run_config import build_agent_test_plan


def regex_config(tmp_path, **changes):
    flags = dict(
        allow_optional_pre_edit_profile=False, enable_regex_search=True, recycle_workspaces=True
    )
    flags.update(changes)
    return factory(tmp_path, **flags)


def test_regex_strategy_is_independent_and_has_unchanged_model_budget_and_fees(tmp_path):
    config = regex_config(tmp_path)
    baseline = regex_config(tmp_path, enable_regex_search=False)
    assert type(config) is SweBenchRegexAgentRunConfig
    assert config.regex_search_enabled and config.workspace_retirement_enabled
    assert not config.grep_snapshots_enabled and not config.optional_pre_edit_profile_enabled
    assert not config.thinking_enabled and not config.planning_enabled
    assert config.provider_config == baseline.provider_config
    assert config.price == baseline.price and config.fee_stop_amount == baseline.fee_stop_amount
    assert config.case_process_profiles == baseline.case_process_profiles
    assert config.worst_case_amount(build_agent_test_plan()) == baseline.worst_case_amount(
        build_agent_test_plan()
    )
    assert config.fingerprint != baseline.fingerprint
    path = tmp_path / "regex.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_regex_search": None},
        {"enable_regex_search": 1},
        {"enable_regex_search": "true"},
        {"recycle_workspaces": False},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
        {"enable_grep_snapshots": True},
        {"allow_optional_pre_edit_profile": True},
        {"enable_thinking": True},
        {"enable_planning": True},
    ],
)
def test_regex_strategy_rejects_implicit_and_mixed_configuration(tmp_path, changes):
    with pytest.raises(ValueError):
        regex_config(tmp_path, **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("search_policy", "pcre_backtracking"),
        ("workspace_policy", "delete_all"),
        ("regex_search_enabled", True),
        ("case_specific_hint", "hidden"),
    ],
)
def test_regex_strategy_rejects_unknown_or_drifted_policy(tmp_path, field, value):
    config = regex_config(tmp_path)
    with pytest.raises(ValueError):
        type(config).model_validate({**config.model_dump(), field: value})
