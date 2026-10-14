"""定义视图独立冻结为v12；旧发布字节、任务、模型和费用边界保持不变。"""

import hashlib
import json
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.run_config import SweBenchDefinitionAgentRunConfig
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from tests.evals.swebench.test_optional_pre_edit_run_config import factory
from tests.evals.swebench.test_run_config import _config, build_agent_test_plan


def definition_config(tmp_path, **changes):
    flags = dict(
        allow_optional_pre_edit_profile=False,
        enable_definition_views=True,
        recycle_workspaces=True,
    )
    flags.update(changes)
    return factory(tmp_path, **flags)


@pytest.mark.parametrize(
    "isolated,pressure,legacy_sha256",
    [
        (False, False, "4ba016265f60dd56d3a5a3c641cdd48354619ce3aa3244f0717dbd279df4e078"),
        (True, False, "e76f9f5270dda6482627ec932af15e0ed2a8153ebe531e367f6f118513c6a0c9"),
        (True, True, "8943a6156394934ee623ce039b74c3a7b87adf841faf2ce5dce448a9ebed3585"),
    ],
)
def test_omitted_and_disabled_definition_views_preserve_frozen_legacy_bytes(
    tmp_path, isolated, pressure, legacy_sha256
):
    default = _config(
        tmp_path,
        amount="10" if isolated else "80",
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
    )
    disabled = factory(
        tmp_path,
        work_root=tmp_path / "runs",
        git_executable=Path("/usr/bin/git"),
        fee_stop_amount=default.fee_stop_amount,
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
        allow_optional_pre_edit_profile=False,
        enable_definition_views=False,
    )
    assert type(default) is type(disabled)
    assert not default.definition_views_enabled and not disabled.definition_views_enabled
    default_path, disabled_path = tmp_path / "default.json", tmp_path / "disabled.json"
    write_swebench_agent_run_config(default_path, default)
    write_swebench_agent_run_config(disabled_path, disabled)
    assert default_path.read_bytes() == disabled_path.read_bytes()
    # 复用旧版快照口径：仅归一化临时路径及绑定该路径的Profile摘要。
    body = default_path.read_text(encoding="utf-8").replace(str(tmp_path.resolve()), "<tmp>")
    for binding in default.case_process_profiles:
        body = body.replace(binding.profile.profile_sha256, "<profile_sha256>")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == legacy_sha256


def test_definition_v12_roundtrips_without_changing_task_model_or_fee_budget(tmp_path):
    config = definition_config(tmp_path)
    baseline = definition_config(tmp_path, enable_definition_views=False)
    assert type(config) is SweBenchDefinitionAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v12"
    assert config.definition_views_enabled and not baseline.definition_views_enabled
    assert config.isolate_provider_failures and config.budget_pressure_compaction
    assert config.workspace_retirement_enabled
    assert not config.regex_search_enabled and not config.closing_phase_enabled
    assert not config.grep_snapshots_enabled and not config.optional_pre_edit_profile_enabled
    assert not config.thinking_enabled and not config.planning_enabled
    assert config.provider_config == baseline.provider_config
    assert config.price == baseline.price and config.billing_context == baseline.billing_context
    assert config.fee_stop_amount == baseline.fee_stop_amount
    assert config.plan_sha256 == baseline.plan_sha256
    assert config.case_process_profiles == baseline.case_process_profiles
    plan = build_agent_test_plan()
    assert config.worst_case_amount(plan) == baseline.worst_case_amount(plan)
    assert config.fingerprint != baseline.fingerprint
    config.require_plan(plan)
    path = tmp_path / "definition.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert type(restored) is SweBenchDefinitionAgentRunConfig
    assert restored == config and restored.fingerprint == config.fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_definition_views": None},
        {"enable_definition_views": 1},
        {"enable_definition_views": "true"},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
        {"recycle_workspaces": False},
        {"enable_regex_search": True},
        {"reserve_closing_phase": True},
        {"enable_grep_snapshots": True},
        {"allow_optional_pre_edit_profile": True},
        {"enable_planning": True},
        {"enable_thinking": True},
    ],
)
def test_definition_factory_rejects_implicit_flags_missing_guards_and_mixed_candidates(
    tmp_path, changes
):
    with pytest.raises(ValueError):
        definition_config(tmp_path, **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("definition_policy", "implicit_all_languages"),
        ("workspace_policy", "delete_all"),
        ("definition_views_enabled", True),
        ("case_specific_hint", "hidden"),
    ],
)
def test_definition_model_and_private_reader_reject_unknown_or_drifted_policy(
    tmp_path, field, value
):
    config = definition_config(tmp_path)
    with pytest.raises(ValueError):
        SweBenchDefinitionAgentRunConfig.model_validate({**config.model_dump(), field: value})
    path = tmp_path / "definition.json"
    write_swebench_agent_run_config(path, config)
    path.write_text(json.dumps({**config.model_dump(mode="json"), field: value}), encoding="utf-8")
    with pytest.raises(KernelError) as error:
        read_swebench_agent_run_config(path)
    assert error.value.code == "swebench_run_config_invalid"
