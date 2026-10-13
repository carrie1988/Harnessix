from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchPreRegistrationPlan,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
    swebench_preregistration_plan_digest,
)
from harnessix.evals.swebench.public_test_profile import (
    SweBenchCaseProcessProfile,
    build_swebench_case_process_profile,
)
from harnessix.evals.swebench.run_config import (
    SweBenchAgentRunConfig,
    SweBenchBudgetWindowAgentRunConfig,
    SweBenchIsolatedAgentRunConfig,
    SweBenchRunConfig,
    SweBenchThinkingAgentRunConfig,
    build_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.models.costs import bind_price, estimate_attempt
from harnessix.models.pricing import FlatInputPrice
from tests.models.pricing_helpers import attempt


def build_agent_test_plan(
    *,
    enable_thinking: bool = False,
    thinking_budget: int | None = None,
    parallel_tool_calls: bool = False,
) -> SweBenchPreRegistrationPlan:
    rows = tuple(
        {
            "instance_id": f"owner__repo-{index}",
            "repo": "owner/repo",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix issue {index}",
            "version": "1.0",
        }
        for index in range(30)
    )
    parameters = [
        SweBenchModelParameter(name="thinking", value="enabled" if enable_thinking else "disabled")
    ]
    if thinking_budget is not None:
        parameters.append(SweBenchModelParameter(name="thinking_budget", value=thinking_budget))
    if parallel_tool_calls:
        parameters.insert(0, SweBenchModelParameter(name="parallel_tool_calls", value=True))
    return build_swebench_preregistration_plan(
        rows,
        dataset_name="SWE-bench/SWE-bench_Verified",
        dataset_revision="1" * 40,
        dataset_artifact_sha256="2" * 64,
        split="test",
        harnessix_revision="3" * 40,
        swebench_revision="4" * 40,
        swebench_package_version="5.0.2",
        task_repo_revision="5" * 40,
        model_name="harnessix-qwen3.8-max",
        model_config=SweBenchModelConfig(
            provider="openai_chat",
            parameters=tuple(parameters),
        ),
        task_budget=SweBenchTaskBudget(
            max_steps=16,
            max_tokens=50_000,
            timeout_seconds=900,
            max_output_chars=65_536,
            max_tool_calls_per_step=8,
        ),
        salt="harnessix-swebench-test-salt-v1",
    )


def _docker(tmp_path: Path, name: str = "docker") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = tmp_path / name
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o700)
    return engine


def _profiles(
    tmp_path: Path, plan: SweBenchPreRegistrationPlan, *, image_offset: int = 0
) -> tuple[SweBenchCaseProcessProfile, ...]:
    engine = _docker(tmp_path)
    return tuple(
        build_swebench_case_process_profile(
            case.instance_id,
            repo=case.repo,
            container_engine=engine,
            image_id=f"sha256:{index + 1 + image_offset:064x}",
        )
        for index, case in enumerate(sorted(plan.cases, key=lambda item: item.instance_id))
    )


def _config(
    tmp_path: Path,
    *,
    amount: str = "80",
    image_offset: int = 0,
    isolate_provider_failures: bool = False,
    budget_pressure_compaction: bool = False,
    enable_thinking: bool = False,
    plan: SweBenchPreRegistrationPlan | None = None,
) -> SweBenchRunConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    plan = plan or build_agent_test_plan(
        enable_thinking=enable_thinking,
        thinking_budget=1024 if enable_thinking else None,
    )
    return build_swebench_agent_run_config(
        plan,
        source_root=root,
        work_root=tmp_path / "runs",
        cache_root=tmp_path / "cache",
        git_executable=Path("/usr/bin/git"),
        case_process_profiles=_profiles(tmp_path, plan, image_offset=image_offset),
        fee_stop_amount=amount,
        observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        isolate_provider_failures=isolate_provider_failures,
        budget_pressure_compaction=budget_pressure_compaction,
        enable_thinking=enable_thinking,
    )


def test_run_config_binds_provider_price_budget_and_separate_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.provider_config.model == "qwen3.8-max"
    assert config.provider_config.max_attempts == 1
    assert config.provider_config.enable_thinking is False
    assert isinstance(config.price.input_price, FlatInputPrice)
    assert config.price.input_price.per_million == "12"
    assert config.price.output_per_million == "36"
    assert config.worst_case_amount(build_agent_test_plan()) == "48.147456"
    assert config.fingerprint == config.fingerprint
    assert tuple(binding.instance_id for binding in config.case_process_profiles) == tuple(
        sorted(case.instance_id for case in build_agent_test_plan().cases)
    )


def test_run_config_fingerprint_covers_complete_case_profiles(tmp_path: Path) -> None:
    first = _config(tmp_path)
    second = SweBenchAgentRunConfig.model_validate(
        {
            **first.model_dump(mode="python"),
            "case_process_profiles": _profiles(
                tmp_path,
                build_agent_test_plan(),
                image_offset=100,
            ),
        },
        strict=True,
    )

    assert first.plan_sha256 == second.plan_sha256
    assert (
        first.case_process_profiles[0].profile.image
        != second.case_process_profiles[0].profile.image
    )
    assert first.fingerprint != second.fingerprint


def test_run_config_rejects_missing_duplicate_and_plan_drifted_profile_mappings(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    payload = config.model_dump(mode="python")

    with pytest.raises(ValueError):
        SweBenchAgentRunConfig.model_validate(
            {**payload, "case_process_profiles": config.case_process_profiles[:-1]},
            strict=True,
        )

    duplicate = (*config.case_process_profiles[:-1], config.case_process_profiles[0])
    with pytest.raises(ValueError, match="排序且唯一"):
        SweBenchAgentRunConfig.model_validate(
            {**payload, "case_process_profiles": duplicate},
            strict=True,
        )

    drifted = tuple(
        sorted(
            (
                *config.case_process_profiles[1:],
                SweBenchCaseProcessProfile(
                    instance_id="other__repo-999",
                    repo="other/repo",
                    profile=config.case_process_profiles[0].profile,
                ),
            ),
            key=lambda binding: binding.instance_id,
        )
    )
    drifted_config = SweBenchAgentRunConfig.model_validate(
        {**payload, "case_process_profiles": drifted},
        strict=True,
    )
    with pytest.raises(ValueError, match="预注册计划"):
        drifted_config.require_plan(build_agent_test_plan())


def test_run_config_rejects_budget_below_proved_worst_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="最坏情况"):
        _config(tmp_path, amount="48")


def test_run_config_rejects_private_root_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ValueError, match="源码树内"):
        build_swebench_agent_run_config(
            (plan := build_agent_test_plan()),
            source_root=source,
            work_root=source / "runs",
            cache_root=tmp_path / "cache",
            git_executable=Path("/usr/bin/git"),
            case_process_profiles=_profiles(tmp_path, plan),
            fee_stop_amount="80",
            observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        )


def test_run_config_is_published_once_as_private_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "run-config.json"

    write_swebench_agent_run_config(path, config)

    assert path.stat().st_mode & 0o777 == 0o600
    assert read_swebench_agent_run_config(path) == config
    with pytest.raises(KernelError, match="已经发布"):
        write_swebench_agent_run_config(path, config)

    os.chmod(path, 0o644)
    with pytest.raises(KernelError, match="权限错误"):
        read_swebench_agent_run_config(path)


@pytest.mark.parametrize(
    "isolated,pressure,model,legacy_sha256",
    [
        (
            False,
            False,
            SweBenchAgentRunConfig,
            "4ba016265f60dd56d3a5a3c641cdd48354619ce3aa3244f0717dbd279df4e078",
        ),
        (
            True,
            False,
            SweBenchIsolatedAgentRunConfig,
            "e76f9f5270dda6482627ec932af15e0ed2a8153ebe531e367f6f118513c6a0c9",
        ),
        (
            True,
            True,
            SweBenchBudgetWindowAgentRunConfig,
            "8943a6156394934ee623ce039b74c3a7b87adf841faf2ce5dce448a9ebed3585",
        ),
    ],
)
def test_budget_window_is_explicit_versioned_and_roundtrips(
    tmp_path: Path,
    isolated: bool,
    pressure: bool,
    model: type[SweBenchRunConfig],
    legacy_sha256: str,
) -> None:
    config = _config(
        tmp_path,
        amount="10" if isolated else "80",
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
    )
    assert type(config) is model
    assert config.budget_pressure_compaction is pressure
    assert config.isolate_provider_failures is isolated
    assert config.thinking_enabled is False
    assert config.output_token_limit_margin == 0
    assert config.provider_config.enable_thinking is False
    assert config.provider_config.thinking_budget is None
    assert config.provider_config.output_token_parameter == "max_tokens"
    assert config.price.inference_mode == config.billing_context.inference_mode == "non-thinking"
    assert config.price.input_tokens_max == 991_808
    assert "thinking_enabled" not in config.model_dump(mode="json")
    assert "output_token_limit_margin" not in config.model_dump(mode="json")
    assert "thinking_budget" not in config.provider_config.model_dump(mode="json")
    config.require_plan(build_agent_test_plan())
    path = tmp_path / "config.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert restored == config and restored.fingerprint == config.fingerprint
    # 旧版原始发布字节快照，仅归一化临时宿主路径及绑定该路径的Profile摘要。
    body = path.read_text(encoding="utf-8").replace(str(tmp_path.resolve()), "<tmp>")
    for binding in config.case_process_profiles:
        body = body.replace(binding.profile.profile_sha256, "<profile_sha256>")
    assert sha256(body.encode("utf-8")).hexdigest() == legacy_sha256
    if pressure:
        assert isinstance(config, SweBenchBudgetWindowAgentRunConfig)
        assert config.context_budget_policy == "amortized_pressure_window"
        payload = config.model_dump(mode="python")
        payload.update(spec_version="harnessix.swebench-agent-run-config/v3")
        with pytest.raises(ValueError):
            SweBenchIsolatedAgentRunConfig.model_validate(payload)


@pytest.mark.parametrize("pressure", [1, None, "true"])
def test_budget_window_rejects_non_boolean_activation(tmp_path: Path, pressure: object) -> None:
    with pytest.raises(ValueError, match="布尔"):
        _config(
            tmp_path,
            isolate_provider_failures=True,
            budget_pressure_compaction=cast(bool, pressure),
        )


def test_budget_window_cannot_silently_change_old_fee_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="逐Case预付"):
        _config(tmp_path, budget_pressure_compaction=True)


def test_parallel_model_tool_calls_require_explicit_preregistration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.provider_config.capabilities.parallel_tool_calls is False
    plan = build_agent_test_plan(parallel_tool_calls=True)
    parallel = build_swebench_agent_run_config(
        plan,
        source_root=Path(config.source_root),
        work_root=Path(config.work_root),
        cache_root=Path(config.cache_root),
        git_executable=Path(config.git_executable),
        case_process_profiles=config.case_process_profiles,
        fee_stop_amount="80",
    )
    assert parallel.provider_config.capabilities.parallel_tool_calls is True
    drifted = parallel.model_copy(update={"provider_config": config.provider_config})
    with pytest.raises(ValueError, match="预注册"):
        drifted.require_plan(plan)


@pytest.mark.parametrize("parallel", [False, True])
def test_thinking_diagnostic_is_explicit_v5_and_roundtrips(tmp_path: Path, parallel: bool) -> None:
    plan = build_agent_test_plan(
        enable_thinking=True, thinking_budget=1024, parallel_tool_calls=parallel
    )
    config = _config(
        tmp_path,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_thinking=True,
        plan=plan,
    )

    assert type(config) is SweBenchThinkingAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v5"
    assert config.thinking_enabled is True
    assert config.output_token_limit_margin == 10
    assert config.isolate_provider_failures is True
    assert config.budget_pressure_compaction is True
    assert config.provider_failure_policy == "freeze_transport_with_full_reserve"
    assert config.fee_admission_policy == "next_isolated_case"
    assert config.context_budget_policy == "amortized_pressure_window"
    assert config.reserve_unaccounted_requests is True
    assert config.provider_config.enable_thinking is True
    assert config.provider_config.thinking_budget == 1024
    assert config.provider_config.output_token_parameter == "max_completion_tokens"
    assert config.provider_config.max_output_tokens == 4096
    assert config.provider_config.max_attempts == 1
    assert config.provider_config.retry_delay_seconds == 0
    assert config.provider_config.timeout_seconds == 900
    assert config.provider_config.io_timeout_seconds == 180
    assert config.provider_config.max_request_bytes == 250_000
    assert config.provider_config.capabilities.parallel_tool_calls is parallel
    assert config.price.inference_mode == config.billing_context.inference_mode == "thinking"
    assert config.price.input_tokens_min == 0
    assert config.price.input_tokens_max == 983_616
    assert config.price.input_price == FlatInputPrice(per_million="12")
    assert config.price.output_per_million == "36"
    assert "thinking_enabled" not in config.model_dump(mode="json")
    assert "output_token_limit_margin" not in config.model_dump(mode="json")
    config.require_plan(plan)

    path = tmp_path / "thinking-config.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert restored == config and restored.fingerprint == config.fingerprint
    assert type(restored) is SweBenchThinkingAgentRunConfig


def test_thinking_mode_and_budget_bind_plan_and_run_fingerprints(tmp_path: Path) -> None:
    original = _config(tmp_path, isolate_provider_failures=True, budget_pressure_compaction=True)
    thinking = _config(
        tmp_path,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_thinking=True,
    )
    plans = (
        build_agent_test_plan(),
        build_agent_test_plan(enable_thinking=True),
        build_agent_test_plan(enable_thinking=True, thinking_budget=1024),
        build_agent_test_plan(enable_thinking=True, thinking_budget=2048),
    )
    assert len({plan.model_name for plan in plans}) == 1
    assert len({plan.plan_sha256 for plan in plans}) == len(plans)
    assert all(plan.plan_sha256 == swebench_preregistration_plan_digest(plan) for plan in plans)
    assert original.plan_sha256 == plans[0].plan_sha256
    assert thinking.plan_sha256 == plans[2].plan_sha256
    assert original.fingerprint != thinking.fingerprint
    assert original.provider_config != thinking.provider_config
    assert original.price.digest != thinking.price.digest


@pytest.mark.parametrize(
    "isolated,pressure,thinking",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
def test_run_versions_reject_opposite_plan_mode(
    tmp_path: Path, isolated: bool, pressure: bool, thinking: bool
) -> None:
    config = _config(
        tmp_path,
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
        enable_thinking=thinking,
    )
    opposite = build_agent_test_plan(
        enable_thinking=not thinking, thinking_budget=None if thinking else 1024
    )
    rebound = config.model_copy(update={"plan_sha256": opposite.plan_sha256})
    with pytest.raises(ValueError, match="预注册计划"):
        rebound.require_plan(opposite)
    with pytest.raises(ValueError, match="预注册计划"):
        _config(
            tmp_path,
            isolate_provider_failures=isolated,
            budget_pressure_compaction=pressure,
            enable_thinking=thinking,
            plan=opposite,
        )


@pytest.mark.parametrize("budget", [None, 0, 1023, 2048])
def test_thinking_plan_requires_exact_explicit_budget(tmp_path: Path, budget: int | None) -> None:
    with pytest.raises(ValueError, match="预注册计划"):
        _config(
            tmp_path,
            isolate_provider_failures=True,
            budget_pressure_compaction=True,
            enable_thinking=True,
            plan=build_agent_test_plan(enable_thinking=True, thinking_budget=budget),
        )


@pytest.mark.parametrize("isolated,pressure", [(False, False), (True, False), (True, True)])
def test_legacy_plans_reject_thinking_budget(
    tmp_path: Path, isolated: bool, pressure: bool
) -> None:
    with pytest.raises(ValueError, match="预注册计划"):
        _config(
            tmp_path,
            isolate_provider_failures=isolated,
            budget_pressure_compaction=pressure,
            plan=build_agent_test_plan(thinking_budget=1024),
        )


@pytest.mark.parametrize("isolated,pressure", [(False, False), (False, True), (True, False)])
def test_thinking_requires_both_explicit_execution_gates(
    tmp_path: Path, isolated: bool, pressure: bool
) -> None:
    with pytest.raises(ValueError, match="显式启用故障隔离与预算窗口"):
        _config(
            tmp_path,
            isolate_provider_failures=isolated,
            budget_pressure_compaction=pressure,
            enable_thinking=True,
        )


@pytest.mark.parametrize("thinking", [1, None, "true"])
def test_thinking_rejects_non_boolean_activation(tmp_path: Path, thinking: object) -> None:
    with pytest.raises(ValueError, match="布尔"):
        _config(
            tmp_path,
            isolate_provider_failures=True,
            budget_pressure_compaction=True,
            enable_thinking=cast(bool, thinking),
        )


@pytest.mark.parametrize(
    "isolated,pressure,thinking",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
@pytest.mark.parametrize("field", ["enable_thinking", "output_token_parameter", "thinking_budget"])
def test_run_versions_reject_provider_mode_drift(
    tmp_path: Path, isolated: bool, pressure: bool, thinking: bool, field: str
) -> None:
    config = _config(
        tmp_path,
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
        enable_thinking=thinking,
    )
    changes = {
        "enable_thinking": not thinking,
        "output_token_parameter": "max_tokens" if thinking else "max_completion_tokens",
        "thinking_budget": None if thinking else 1024,
    }
    provider = config.provider_config.model_copy(update={field: changes[field]})
    with pytest.raises(ValueError, match="固定无重试工具边界|思考预算要求"):
        type(config).model_validate(
            {**config.model_dump(mode="python"), "provider_config": provider}, strict=True
        )


@pytest.mark.parametrize("budget", [None, 0, 1023, 2048])
def test_thinking_provider_requires_fixed_budget(tmp_path: Path, budget: int | None) -> None:
    config = _config(
        tmp_path,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_thinking=True,
    )
    provider = config.provider_config.model_copy(update={"thinking_budget": budget})
    with pytest.raises(ValueError, match="固定无重试工具边界"):
        SweBenchThinkingAgentRunConfig.model_validate(
            {**config.model_dump(mode="python"), "provider_config": provider}, strict=True
        )


@pytest.mark.parametrize(
    "isolated,pressure,thinking",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
@pytest.mark.parametrize("price_drift,billing_drift", [(True, False), (False, True), (True, True)])
def test_run_versions_reject_price_and_billing_mode_drift(
    tmp_path: Path,
    isolated: bool,
    pressure: bool,
    thinking: bool,
    price_drift: bool,
    billing_drift: bool,
) -> None:
    config = _config(
        tmp_path,
        isolate_provider_failures=isolated,
        budget_pressure_compaction=pressure,
        enable_thinking=thinking,
    )
    opposite = {"inference_mode": "non-thinking" if thinking else "thinking"}
    payload = config.model_dump(mode="python")
    if price_drift:
        payload["price"] = config.price.model_copy(update=opposite)
    if billing_drift:
        payload["billing_context"] = config.billing_context.model_copy(update=opposite)
    with pytest.raises(ValueError, match="价格或计费上下文"):
        type(config).model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens_min": 1},
        {"input_tokens_max": 991_808},
        {"input_price": FlatInputPrice(per_million="11")},
        {"output_per_million": "35"},
    ],
)
def test_thinking_rejects_unfrozen_price_band_or_rates(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    config = _config(
        tmp_path,
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_thinking=True,
    )
    with pytest.raises(ValueError, match="价格或计费上下文"):
        SweBenchThinkingAgentRunConfig.model_validate(
            {
                **config.model_dump(mode="python"),
                "price": config.price.model_copy(update=changes),
            },
            strict=True,
        )


@pytest.mark.parametrize("reasoning_tokens", [None, 0, 1024])
def test_thinking_cost_and_worst_case_count_reasoning_once(
    tmp_path: Path, reasoning_tokens: int | None
) -> None:
    config = _config(
        tmp_path,
        amount="4.948176",
        isolate_provider_failures=True,
        budget_pressure_compaction=True,
        enable_thinking=True,
    )
    plan = build_agent_test_plan(enable_thinking=True, thinking_budget=1024)
    source = cast(Callable[..., ModelAttempt], attempt)(
        requested_model=config.provider_config.model,
        actual_model=config.provider_config.model,
        started_at=config.price.valid_from,
        finished_at=config.price.valid_from + timedelta(seconds=1),
        usage=UsageObservation(
            completeness="complete",
            input_tokens=config.provider_config.max_request_bytes,
            output_tokens=config.provider_config.max_output_tokens
            + config.output_token_limit_margin,
            reasoning_output_tokens=reasoning_tokens,
        ),
    )
    result = estimate_attempt(
        source, bind_price(source, config.price, config.billing_context)
    ).result
    assert result.status == "estimated"
    assert result.amount == "3.147816"
    assert [line.category for line in result.lines] == ["input", "output"]
    assert result.lines[-1].tokens == 4106
    assert result.lines[-1].amount == "0.147816"
    # 25 * ((50000 + 10) * 36 + 250000 * 12 + (4096 + 10) * 36) / 1000000。
    assert config.worst_case_amount(plan) == "123.7044"
    assert plan.task_budget.max_tokens == 50_000
    config.require_plan(plan)
    with pytest.raises(ValueError, match="最坏情况"):
        _config(
            tmp_path,
            amount="4.948175999999999999",
            isolate_provider_failures=True,
            budget_pressure_compaction=True,
            enable_thinking=True,
        )
