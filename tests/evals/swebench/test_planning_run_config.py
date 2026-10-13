from collections.abc import Callable
from functools import partial
from hashlib import sha256
from pathlib import Path

import pytest

from harnessix.evals.swebench.run_config import (
    SweBenchPlanningAgentRunConfig,
    SweBenchRunConfig,
    build_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.models.pricing import amount_units
from tests.evals.swebench.test_run_config import _config, build_agent_test_plan


@pytest.fixture
def factory(tmp_path: Path) -> Callable[..., SweBenchRunConfig]:
    base = _config(tmp_path)
    return partial(
        build_swebench_agent_run_config,
        plan=build_agent_test_plan(),
        source_root=Path(base.source_root),
        work_root=Path(base.work_root),
        cache_root=Path(base.cache_root),
        git_executable=Path(base.git_executable),
        case_process_profiles=base.case_process_profiles,
        fee_stop_amount=base.fee_stop_amount,
        observed_at=base.price.valid_from,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_planning=True,
    )


@pytest.mark.parametrize("parallel", [False, True])
def test_planning_factory_roundtrip_and_fingerprint(
    tmp_path: Path, factory, parallel: bool
) -> None:
    plan = build_agent_test_plan(parallel_tool_calls=parallel)
    config = factory(plan=plan)
    baseline = factory(plan=plan, enable_planning=False)
    assert type(config) is SweBenchPlanningAgentRunConfig
    assert config.planning_enabled and config.isolate_provider_failures
    assert config.budget_pressure_compaction and not config.thinking_enabled
    assert config.planning_policy == "native-model-plan"
    assert config.model_dump(exclude={"spec_version", "planning_policy"}) == baseline.model_dump(
        exclude={"spec_version"}
    )
    assert config.fingerprint != baseline.fingerprint
    assert (
        config.model_copy(update={"planning_policy": "tampered"}).fingerprint != config.fingerprint
    )
    assert (
        plan.task_budget.max_tokens,
        plan.task_budget.max_steps,
        plan.task_budget.timeout_seconds,
    ) == (50_000, 16, 900)
    config.require_plan(plan)
    path = tmp_path / "planning.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert type(restored) is SweBenchPlanningAgentRunConfig
    assert restored == config and restored.fingerprint == config.fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_planning": 1},
        {"enable_planning": None},
        {"enable_planning": "true"},
        {"isolate_provider_failures": False},
        {"budget_pressure_compaction": False},
        {"enable_thinking": True},
        {"plan": build_agent_test_plan(enable_thinking=True, thinking_budget=1024)},
        {"plan": build_agent_test_plan(thinking_budget=1024)},
    ],
)
def test_planning_rejects_implicit_gates_and_thinking(factory, changes) -> None:
    with pytest.raises(ValueError):
        factory(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"planning_policy": "tampered"},
        {"planning_enabled": True},
        {"unknown": True},
        {"spec_version": "harnessix.swebench-agent-run-config/v4"},
    ],
)
def test_planning_rejects_tampered_and_unknown_fields(factory, changes) -> None:
    config = factory()
    with pytest.raises(ValueError):
        SweBenchPlanningAgentRunConfig.model_validate({**config.model_dump(), **changes})


@pytest.mark.parametrize(
    "isolated,pressure,thinking,expected",
    [
        (False, False, False, "4ba016265f60dd56d3a5a3c641cdd48354619ce3aa3244f0717dbd279df4e078"),
        (True, False, False, "314bacb35dfb0c49be2a6ce14f2884089320bd8264d474292ca98be50430d406"),
        (True, True, False, "9c5fa8f267fec4eb22728159448466e9406fee2ec31054a1cca71b93e1296375"),
        (True, True, True, "0d8047d8f110815a7cf7925be589fa98ec413aac62d1242d3bc8b5eae6865717"),
    ],
)
def test_legacy_serialization_and_fingerprints_unchanged(
    tmp_path, isolated, pressure, thinking, expected
):
    config = _config(
        tmp_path,
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
        enable_thinking=thinking,
    )
    assert not config.planning_enabled
    assert config.output_token_limit_margin == (10 if thinking else 0)
    assert (
        "planning_enabled" not in config.model_dump()
        and "planning_policy" not in config.model_dump()
    )
    # 实施前的发布字节快照，只归一化临时路径及绑定该路径的Profile摘要。
    body = (config.model_dump_json(indent=2) + "\n").replace(str(tmp_path.resolve()), "<tmp>")
    for binding in config.case_process_profiles:
        body = body.replace(binding.profile.profile_sha256, "<profile_sha256>")
    assert sha256(body.encode()).hexdigest() == expected
    assert (
        type(config).model_validate_json(config.model_dump_json()).fingerprint == config.fingerprint
    )


def test_planning_fee_margin_does_not_expand_task_budget(factory) -> None:
    config = factory(fee_stop_amount="4.948176")
    baseline = factory(enable_planning=False)
    plan = build_agent_test_plan()
    assert config.output_token_limit_margin == 10
    # 25 * ((50000 + 10) * 36 + 250000 * 12 + (4096 + 10) * 36) / 1000000。
    assert config.worst_case_amount(plan) == "123.7044"
    assert amount_units(config.worst_case_amount(plan)) - amount_units(
        baseline.worst_case_amount(plan)
    ) == amount_units("0.018")
    config.require_plan(plan)
    with pytest.raises(ValueError, match="最坏情况"):
        factory(fee_stop_amount="4.948175999999999999")
