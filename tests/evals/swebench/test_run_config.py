from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
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
    build_swebench_agent_run_config,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)


def build_agent_test_plan():
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
            parameters=(SweBenchModelParameter(name="thinking", value="disabled"),),
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


def _profiles(tmp_path: Path, plan, *, image_offset: int = 0):
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


def _config(tmp_path: Path, *, amount: str = "80", image_offset: int = 0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "source"
    root.mkdir()
    plan = build_agent_test_plan()
    return build_swebench_agent_run_config(
        plan,
        source_root=root,
        work_root=tmp_path / "runs",
        cache_root=tmp_path / "cache",
        git_executable=Path("/usr/bin/git"),
        case_process_profiles=_profiles(tmp_path, plan, image_offset=image_offset),
        fee_stop_amount=amount,
        observed_at=datetime(2026, 10, 12, tzinfo=UTC),
    )


def test_run_config_binds_provider_price_budget_and_separate_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.provider_config.model == "qwen3.8-max"
    assert config.provider_config.max_attempts == 1
    assert config.provider_config.enable_thinking is False
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


def test_parallel_model_tool_calls_require_explicit_preregistration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.provider_config.capabilities.parallel_tool_calls is False
    plan = build_agent_test_plan()
    settings = plan.model_settings.model_copy(
        update={
            "parameters": (
                SweBenchModelParameter(name="parallel_tool_calls", value=True),
                SweBenchModelParameter(name="thinking", value="disabled"),
            )
        }
    )
    candidate = plan.model_copy(update={"model_settings": settings, "plan_sha256": "0" * 64})
    plan = candidate.model_copy(
        update={"plan_sha256": swebench_preregistration_plan_digest(candidate)}
    )
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
