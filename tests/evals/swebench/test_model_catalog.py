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
from harnessix.evals.swebench.run_settings import BAILIAN_MODELS
from harnessix.models.pricing import FlatInputPrice
from tests.evals.swebench.test_compact_surface_run_config import compact_config
from tests.evals.swebench.test_run_config import build_agent_test_plan

V17 = {"convergence_guidance": True, "patch_deadline_requests": 4, "budget_capped_truncation": True}


def plan_for(model: str) -> SweBenchPreRegistrationPlan:
    plan = build_agent_test_plan().model_copy(
        update={"model_name": BAILIAN_MODELS[model].public_name}
    )
    return SweBenchPreRegistrationPlan.model_validate(
        {
            **plan.model_dump(mode="python"),
            "plan_sha256": swebench_preregistration_plan_digest(plan),
        }
    )


@pytest.mark.parametrize("model", sorted(BAILIAN_MODELS))
def test_each_model_binds_its_verified_price_and_public_name(tmp_path, model):
    plan = plan_for(model)
    config = compact_config(tmp_path, **V17, model=model, plan=plan)
    entry = BAILIAN_MODELS[model]
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
