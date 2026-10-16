"""已求证模型目录：模型名、价格、阶梯上界与计划公开名一一绑定；默认qwen3.8-max不变。"""

import json

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import (
    SweBenchPreRegistrationPlan,
    swebench_preregistration_plan_digest,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_settings import MODEL_CATALOG
from harnessix.models.pricing import FlatInputPrice
from tests.evals.swebench.test_compact_surface_run_config import compact_config
from tests.evals.swebench.test_run_config import build_agent_test_plan

V17 = {"convergence_guidance": True, "patch_deadline_requests": 4, "budget_capped_truncation": True}


def plan_for(model: str) -> SweBenchPreRegistrationPlan:
    plan = build_agent_test_plan().model_copy(
        update={"model_name": MODEL_CATALOG[model].public_name}
    )
    return SweBenchPreRegistrationPlan.model_validate(
        {
            **plan.model_dump(mode="python"),
            "plan_sha256": swebench_preregistration_plan_digest(plan),
        }
    )


@pytest.mark.parametrize("model", sorted(MODEL_CATALOG))
def test_each_model_binds_its_verified_price_and_public_name(tmp_path, model):
    plan = plan_for(model)
    config = compact_config(tmp_path, **V17, model=model, plan=plan)
    entry = MODEL_CATALOG[model]
    assert config.provider_config.model == config.price.model == model
    assert config.price.input_price == FlatInputPrice(per_million=entry.input_price)
    assert config.price.output_per_million == entry.output_price
    assert config.price.input_tokens_max == entry.input_tokens_max
    assert config.price.source_url == entry.price_url
    config.require_plan(plan)


def test_default_model_is_unchanged(tmp_path):
    config = compact_config(tmp_path, **V17)
    assert config.provider_config.model == "qwen3.8-max"
    assert config.model.public_name == "harnessix-qwen3.8-max"


def test_plan_public_name_must_match_the_selected_model(tmp_path):
    config = compact_config(tmp_path, **V17, model="qwen3.7-plus", plan=plan_for("qwen3.7-plus"))
    with pytest.raises(ValueError):
        config.require_plan(plan_for("qwen3.8-max"))


def test_cheaper_model_lowers_the_frozen_worst_case(tmp_path):
    plus = compact_config(tmp_path, **V17, model="qwen3.7-plus", plan=plan_for("qwen3.7-plus"))
    max_ = compact_config(tmp_path, **V17)
    assert float(plus.worst_case_amount(plan_for("qwen3.7-plus"))) < float(
        max_.worst_case_amount(build_agent_test_plan())
    )


@pytest.mark.parametrize(
    "update",
    [
        {"provider_config": {"model": "qwen-unknown"}, "price": {"model": "qwen-unknown"}},
        {"price": {"output_per_million": "36"}},
        {"price": {"input_tokens_max": 991_808}},
    ],
)
def test_tampered_model_or_price_is_rejected(tmp_path, update):
    config = compact_config(tmp_path, **V17, model="qwen3.7-plus", plan=plan_for("qwen3.7-plus"))
    data = config.model_dump(mode="json")
    control = tmp_path / "control.json"
    control.write_text(json.dumps(data))
    control.chmod(0o600)
    assert read_swebench_agent_run_config(control) == config
    for key, fields in update.items():
        data[key] = {**data[key], **fields}
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises((ValueError, ValidationError, KernelError)):
        read_swebench_agent_run_config(path)


def test_prompt_cache_v18_binds_cache_layout_and_partitioned_price(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchPromptCacheAgentRunConfig
    from harnessix.models.pricing import PartitionedInputPrice

    config = compact_config(tmp_path, **V17, prompt_cache=True)
    v17 = compact_config(tmp_path, **V17)
    assert type(config) is SweBenchPromptCacheAgentRunConfig and config.prompt_cache
    assert config.provider_config.prompt_cache == "explicit_tail"
    assert config.price.input_price == PartitionedInputPrice(
        uncached_per_million="12",
        cache_read_per_million="1.5",
        cache_creation_per_million="15",
        cache_write_ttl=None,
    )
    assert config.normalize_stringified_arguments and not v17.normalize_stringified_arguments
    excluded = {"spec_version", "cache_policy", "argument_policy", "provider_config", "price"}
    assert config.model_dump(mode="json", exclude=excluded) == v17.model_dump(
        mode="json", exclude=excluded
    )
    # 缓存创建价高于未缓存价，最坏费用随之上调，绝不因缓存而放宽。
    plan = build_agent_test_plan()
    assert float(config.worst_case_amount(plan)) > float(v17.worst_case_amount(plan))


@pytest.mark.parametrize(
    "update",
    [
        {"provider_config": {"prompt_cache": None}},
        {"price": {"input_price": {"kind": "flat", "per_million": "12"}}},
    ],
)
def test_prompt_cache_requires_both_layout_and_price(tmp_path, update):
    config = compact_config(tmp_path, **V17, prompt_cache=True)
    data = config.model_dump(mode="json")
    for key, fields in update.items():
        data[key] = {**data[key], **fields}
        if data[key].get("prompt_cache") is None:
            data[key].pop("prompt_cache", None)
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises((ValueError, ValidationError, KernelError)):
        read_swebench_agent_run_config(path)


def test_prompt_cache_requires_the_v17_stack(tmp_path):
    with pytest.raises(ValueError):
        compact_config(
            tmp_path, convergence_guidance=True, patch_deadline_requests=4, prompt_cache=True
        )


def test_full_cached_history_v19_drops_the_working_set_only(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchFullCachedHistoryAgentRunConfig

    config = compact_config(tmp_path, **V17, prompt_cache=True, working_set_result_groups=None)
    v18 = compact_config(tmp_path, **V17, prompt_cache=True)
    assert type(config) is SweBenchFullCachedHistoryAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v19"
    assert config.working_set_result_groups is None and v18.working_set_result_groups == 2
    assert config.prompt_cache and config.normalize_stringified_arguments
    excluded = {"spec_version", "history_view_policy", "retain_recent_result_groups"}
    assert config.model_dump(mode="json", exclude=excluded) == v18.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v19.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


def test_without_prompt_cache_the_working_set_is_still_required(tmp_path):
    with pytest.raises(ValueError):
        compact_config(tmp_path, **V17, working_set_result_groups=None)


def test_cached_closure_v20_adds_catalog_reopen_and_drops_pressure_window(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchCachedClosureAgentRunConfig

    common = {**V17, "prompt_cache": True, "working_set_result_groups": None}
    config = compact_config(
        tmp_path, **common, cached_closure_workflow=True, budget_pressure_compaction=False
    )
    v19 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchCachedClosureAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v20"
    assert config.stable_tool_catalog and config.reopen_after_clean_review
    assert not config.budget_pressure_compaction and v19.budget_pressure_compaction
    assert not v19.stable_tool_catalog and not v19.reopen_after_clean_review
    excluded = {"spec_version", "context_budget_policy", "catalog_policy", "workflow_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v19.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v20.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_pressure_compaction": True},
        {"working_set_result_groups": 2},
        {"prompt_cache": False, "working_set_result_groups": 2},
    ],
)
def test_cached_closure_requires_cache_without_working_set_or_pressure(tmp_path, changes):
    settings = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        **changes,
    }
    with pytest.raises(ValueError):
        compact_config(tmp_path, **settings)


def test_scratch_reproduction_v21_adds_only_the_reproduction_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchScratchReproductionAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
    }
    config = compact_config(tmp_path, **common, scratch_reproduction=True)
    v20 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchScratchReproductionAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v21"
    assert config.scratch_reproduction and not v20.scratch_reproduction
    excluded = {"spec_version", "reproduction_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v20.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v21.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(tmp_path, **V17, prompt_cache=True, scratch_reproduction=True)


def test_protocol_robust_v22_adds_only_isolation_and_call_cap(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchProtocolRobustAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
    }
    config = compact_config(tmp_path, **common, protocol_robust=True)
    v21 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchProtocolRobustAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v22"
    assert config.isolate_protocol_failures and config.state_tool_call_cap
    assert not v21.isolate_protocol_failures and not v21.state_tool_call_cap
    excluded = {"spec_version", "provider_failure_policy", "tool_call_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v21.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v22.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(tmp_path, **{**common, "scratch_reproduction": False}, protocol_robust=True)


def test_explained_gates_v23_adds_only_the_tool_gate_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchExplainedGatesAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
    }
    config = compact_config(tmp_path, **common, explained_gates=True)
    v22 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchExplainedGatesAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v23"
    assert config.explain_tool_gates and not v22.explain_tool_gates
    excluded = {"spec_version", "tool_gate_policy", "reserve_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v22.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v23.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(tmp_path, **{**common, "protocol_robust": False}, explained_gates=True)


def test_v23_bounds_the_output_share_of_the_case_reserve(tmp_path):
    from harnessix.models.pricing import max_input_rate, rate_units

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
    }
    v23 = compact_config(tmp_path, **common, explained_gates=True)
    v22 = compact_config(tmp_path, **common)
    budget = build_agent_test_plan().task_budget
    rates = {
        "input_rate": max_input_rate(v23.price.input_price),
        "output_rate": rate_units(v23.price.output_per_million),
    }
    total = budget.max_tokens
    assert v22.known_case_reserve_units(budget, **rates) == total * rates["output_rate"]
    output = min(total, budget.max_steps * v23.provider_config.max_output_tokens)
    assert v23.known_case_reserve_units(budget, **rates) == (
        output * rates["output_rate"] + (total - output) * rates["input_rate"]
    )
    assert v23.known_case_reserve_units(budget, **rates) <= v22.known_case_reserve_units(
        budget, **rates
    )


def test_deepseek_v23_uses_automatic_cache_and_its_own_endpoint(tmp_path):
    from harnessix.models.pricing import PartitionedInputPrice

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
    }
    plan = plan_for("deepseek-flash")
    config = compact_config(tmp_path, **common, model="deepseek-flash", plan=plan)
    provider = config.provider_config
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.prompt_cache is None and config.prompt_cache
    assert provider.thinking_format == "thinking_type" and provider.enable_thinking is False
    assert config.price.billing_provider == config.billing_context.billing_provider == "deepseek"
    assert config.price.input_price == PartitionedInputPrice(
        uncached_per_million="2",
        cache_read_per_million="0.04",
        cache_creation_per_million="2",
        cache_write_ttl=None,
    )
    config.require_plan(plan)
    path = tmp_path / "deepseek.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


def test_connect_retry_v24_adds_only_the_connection_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchConnectRetryAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
    }
    config = compact_config(tmp_path, **common, connect_retry=True)
    v23 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchConnectRetryAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v24"
    assert config.provider_config.connect_retries == 3
    assert v23.provider_config.connect_retries == 0
    excluded = {"spec_version", "connection_policy", "provider_config"}
    assert config.model_dump(mode="json", exclude=excluded) == v23.model_dump(
        mode="json", exclude=excluded
    )
    assert config.provider_config.model_copy(update={"connect_retries": 0}) == v23.provider_config
    path = tmp_path / "v24.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(tmp_path, **{**common, "explained_gates": False}, connect_retry=True)


def test_forced_choice_tolerant_v25_adds_only_the_forced_choice_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchForcedChoiceTolerantAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
        "connect_retry": True,
    }
    config = compact_config(tmp_path, **common, forced_choice_tolerant=True)
    v24 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchForcedChoiceTolerantAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v25"
    assert config.reject_forced_choice_violations and not v24.reject_forced_choice_violations
    excluded = {"spec_version", "forced_choice_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v24.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v25.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(tmp_path, **{**common, "connect_retry": False}, forced_choice_tolerant=True)


def test_argument_repair_v26_adds_only_the_argument_wire_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchArgumentRepairAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
        "connect_retry": True,
        "forced_choice_tolerant": True,
    }
    config = compact_config(tmp_path, **common, argument_repair=True)
    v25 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchArgumentRepairAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v26"
    assert config.provider_config.repair_tool_arguments
    assert not v25.provider_config.repair_tool_arguments
    excluded = {"spec_version", "argument_wire_policy", "cache_layout_policy", "provider_config"}
    assert config.model_dump(mode="json", exclude=excluded) == v25.model_dump(
        mode="json", exclude=excluded
    )
    assert (
        config.provider_config.model_copy(update={"repair_tool_arguments": False})
        == v25.provider_config
    )
    path = tmp_path / "v26.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(ValueError):
        compact_config(
            tmp_path, **{**common, "forced_choice_tolerant": False}, argument_repair=True
        )


def test_deepseek_v26_uses_the_stable_tail_layout(tmp_path):
    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
        "connect_retry": True,
        "forced_choice_tolerant": True,
    }
    plan = plan_for("deepseek-flash")
    deepseek = {"model": "deepseek-flash", "plan": plan}
    v26 = compact_config(tmp_path, **common, argument_repair=True, **deepseek)
    v25 = compact_config(tmp_path, **common, **deepseek)
    assert v26.provider_config.prompt_cache == "stable_tail"
    assert v25.provider_config.prompt_cache is None
    # 显式缓存模型不受影响。
    qwen = compact_config(tmp_path, **common, argument_repair=True)
    assert qwen.provider_config.prompt_cache == "explicit_tail"


def test_test_timeout_v27_adds_only_the_timeout_policy(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchTestTimeoutAgentRunConfig

    common = {
        **V17,
        "prompt_cache": True,
        "working_set_result_groups": None,
        "cached_closure_workflow": True,
        "budget_pressure_compaction": False,
        "scratch_reproduction": True,
        "protocol_robust": True,
        "explained_gates": True,
        "connect_retry": True,
        "forced_choice_tolerant": True,
        "argument_repair": True,
    }
    config = compact_config(tmp_path, **common, test_timeout_failures=True)
    v26 = compact_config(tmp_path, **common)
    assert type(config) is SweBenchTestTimeoutAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v27"
    assert config.test_timeouts_are_failures and not v26.test_timeouts_are_failures
    excluded = {"spec_version", "test_timeout_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v26.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v27.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
