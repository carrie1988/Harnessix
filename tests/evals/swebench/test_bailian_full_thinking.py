"""百炼DeepSeek（闲时单价、隐式缓存口径、计价时段准入）与v39全程思考：只改思考开关与输出上限。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.evals.swebench.execution_costs import _case_fits_billing_window
from harnessix.evals.swebench.public_test_profile import build_swebench_case_process_profile
from harnessix.evals.swebench.run_config import (
    SweBenchFullThinkingAgentRunConfig,
    SweBenchWideOutputAgentRunConfig,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_settings import MODEL_CATALOG
from harnessix.models.pricing import PartitionedInputPrice
from tests.evals.swebench.test_compact_surface_run_config import compact_config
from tests.evals.swebench.test_model_catalog import V17, _thinking_plan, plan_for

MODEL = "deepseek-v4-pro-0813"
V36_STACK = {
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
    "test_timeout_failures": True,
    "reads_after_failed_patch": True,
    "truncation_quality": True,
    "self_verification": True,
    "bounded_repair": True,
    "clean_review_notice": True,
    "provider_proxy_url": "http://127.0.0.1:7890",
}
V37 = {**V36_STACK, "repair_closure": True, "sibling_scope": True, "wide_output": True}


def _native_profiles(tmp_path, model=MODEL):
    base = compact_config(tmp_path, **V36_STACK, model=model, plan=plan_for(model))
    return tuple(
        build_swebench_case_process_profile(
            binding.instance_id,
            repo=binding.repo,
            container_engine=Path(binding.profile.container_engine),
            image_id=binding.profile.image,
            writable_snapshot=True,
            require_native_selectors=True,
        )
        for binding in base.case_process_profiles
    )


def _config(tmp_path, *, model=MODEL, **changes):
    return compact_config(
        tmp_path,
        **V37,
        case_process_profiles=_native_profiles(tmp_path, model),
        model=model,
        **changes,
    )


def test_bailian_v37_binds_idle_prices_and_the_implicit_cache_usage(tmp_path):
    config = _config(tmp_path, plan=plan_for(MODEL))
    provider = config.provider_config
    assert type(config) is SweBenchWideOutputAgentRunConfig
    assert provider.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert provider.enable_thinking is False and provider.thinking_format is None
    assert provider.prompt_cache == "stable_tail" and provider.implicit_cache_usage
    assert provider.omit_tool_choice and provider.proxy_url == "http://127.0.0.1:7890"
    assert config.price.input_price == PartitionedInputPrice(
        uncached_per_million="4.5",
        cache_read_per_million="0.9",
        cache_creation_per_million="4.5",
        cache_write_ttl=None,
    )
    assert config.price.output_per_million == "13.5"
    assert config.price.billing_provider == "aliyun-bailian"
    assert config.billing_window == MODEL_CATALOG[MODEL].billing_window
    path = tmp_path / "bailian-v37.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


def test_direct_deepseek_keeps_its_reported_split_and_has_no_billing_window(tmp_path):
    config = _config(tmp_path, model="deepseek-v4-pro", plan=plan_for("deepseek-v4-pro"))
    provider = config.provider_config
    assert not provider.implicit_cache_usage and not provider.omit_tool_choice
    assert config.billing_window is None
    assert not {"implicit_cache_usage", "omit_tool_choice"} & set(provider.model_dump(mode="json"))


def test_full_thinking_v39_changes_only_thinking_and_output_limits(tmp_path):
    plan = _thinking_plan(MODEL, "enabled")
    config = _config(tmp_path, full_thinking=True, plan=plan)
    v37 = _config(tmp_path, plan=plan_for(MODEL))
    provider = config.provider_config
    assert type(config) is SweBenchFullThinkingAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v39"
    assert config.full_thinking and not v37.full_thinking
    assert provider.enable_thinking is True and provider.thinking_budget is None
    assert provider.thinking_format is None and provider.output_token_parameter == "max_tokens"
    assert provider.max_output_tokens == 16_384 and provider.max_chunks == 40_000
    assert provider.max_response_bytes == 12 * 1024 * 1024
    assert config.price.inference_mode == config.billing_context.inference_mode == "thinking"
    restored = provider.model_copy(
        update={
            "enable_thinking": False,
            "max_output_tokens": 8192,
            "max_chunks": 10_000,
            "max_response_bytes": 4 * 1024 * 1024,
        }
    )
    assert restored == v37.provider_config
    excluded = {
        "spec_version",
        "thinking_policy",
        "plan_sha256",
        "provider_config",
        "price",
        "billing_context",
    }
    assert config.model_dump(mode="json", exclude=excluded) == v37.model_dump(
        mode="json", exclude=excluded
    )
    assert config.worst_case_amount(plan) > v37.worst_case_amount(plan_for(MODEL))
    config.require_plan(plan)
    with pytest.raises(ValueError):
        config.require_plan(plan_for(MODEL))
    path = tmp_path / "v39.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "provider_update",
    [
        {"max_output_tokens": 8192},
        {"max_chunks": 10_000},
        {"enable_thinking": False},
        {"omit_tool_choice": False},
    ],
)
def test_full_thinking_rejects_tampered_wire_limits(tmp_path, provider_update):
    config = _config(tmp_path, full_thinking=True, plan=_thinking_plan(MODEL, "enabled"))
    data = config.model_dump(mode="python")
    data["provider_config"] = {**data["provider_config"], **provider_update}
    with pytest.raises(ValidationError):
        SweBenchFullThinkingAgentRunConfig.model_validate(data)


def test_full_thinking_needs_an_endpoint_that_keeps_forced_tool_choice(tmp_path):
    # DeepSeek直连的思考模式只接受tool_choice=auto，工作流的强制选择会被丢弃。
    with pytest.raises((ValueError, ValidationError)):
        _config(
            tmp_path,
            model="deepseek-v4-pro",
            full_thinking=True,
            plan=_thinking_plan("deepseek-v4-pro", "enabled"),
        )


@pytest.mark.parametrize(
    "changes", [{"reproduction_gate": True}, {"wide_output": False}, {"native_thinking": True}]
)
def test_full_thinking_is_built_only_over_v37_alone(tmp_path, changes):
    flags = {**V37, **changes}
    with pytest.raises((ValueError, ValidationError)):
        compact_config(
            tmp_path,
            **flags,
            full_thinking=True,
            case_process_profiles=_native_profiles(tmp_path),
            model=MODEL,
            plan=_thinking_plan(MODEL, "enabled"),
        )


@pytest.mark.parametrize(
    ("utc", "minutes"),
    [
        ("2026-10-27T14:00:00", 600),  # 北京22:00开窗
        ("2026-10-27T16:00:00", 480),  # 北京00:00跨午夜
        ("2026-10-27T23:30:00", 30),  # 北京07:30
        ("2026-10-28T00:00:00", None),  # 北京08:00关窗（不含）
        ("2026-10-27T13:59:00", None),  # 北京21:59
    ],
)
def test_billing_window_crosses_midnight_in_beijing_time(utc, minutes):
    window = MODEL_CATALOG[MODEL].billing_window
    remaining = window.remaining(datetime.fromisoformat(utc).replace(tzinfo=UTC))
    assert remaining == (None if minutes is None else timedelta(minutes=minutes))


def test_a_case_starts_only_when_its_whole_wall_clock_fits_the_window(tmp_path):
    plan = plan_for(MODEL)
    config = _config(tmp_path, plan=plan)
    closes = datetime(2026, 10, 28, 0, tzinfo=UTC)  # 北京08:00
    needed = timedelta(seconds=plan.task_budget.timeout_seconds) + timedelta(minutes=10)
    assert _case_fits_billing_window(plan, config, closes - needed)
    assert not _case_fits_billing_window(plan, config, closes - needed + timedelta(minutes=1))
    assert not _case_fits_billing_window(plan, config, datetime(2026, 10, 27, 6, tzinfo=UTC))
    direct = _config(tmp_path, model="deepseek-v4-pro", plan=plan_for("deepseek-v4-pro"))
    assert _case_fits_billing_window(plan, direct, datetime(2026, 10, 27, 6, tzinfo=UTC))


def test_tool_step_thinking_v40_only_scopes_thinking_and_widens_output(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchToolStepThinkingAgentRunConfig

    plan = _thinking_plan(MODEL, "enabled")
    config = _config(tmp_path, full_thinking=True, tool_step_thinking=True, plan=plan)
    v39 = _config(tmp_path, full_thinking=True, plan=plan)
    provider = config.provider_config
    assert type(config) is SweBenchToolStepThinkingAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v40"
    assert config.thinking_tool_steps_only and not v39.thinking_tool_steps_only
    assert provider.thinking_tool_steps_only and provider.max_output_tokens == 32_768
    assert provider.max_chunks == 80_000 and provider.max_response_bytes == 12 * 1024 * 1024
    restored = provider.model_copy(
        update={
            "thinking_tool_steps_only": False,
            "max_output_tokens": 16_384,
            "max_chunks": 40_000,
        }
    )
    assert restored == v39.provider_config
    config.require_plan(plan)
    path = tmp_path / "v40.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config
    data = config.model_dump(mode="python")
    data["provider_config"] = {**data["provider_config"], "max_output_tokens": 16_384}
    with pytest.raises(ValidationError):
        SweBenchToolStepThinkingAgentRunConfig.model_validate(data)
    with pytest.raises((ValueError, ValidationError)):
        _config(tmp_path, tool_step_thinking=True, plan=plan_for(MODEL))
