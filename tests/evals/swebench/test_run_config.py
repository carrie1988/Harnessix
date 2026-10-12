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
)
from harnessix.evals.swebench.run_config import build_swebench_agent_run_config
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)


def _plan():
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


def _config(tmp_path: Path, *, amount: str = "80"):
    root = tmp_path / "source"
    root.mkdir()
    return build_swebench_agent_run_config(
        _plan(),
        source_root=root,
        work_root=tmp_path / "runs",
        cache_root=tmp_path / "cache",
        git_executable=Path("/usr/bin/git"),
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
    assert config.worst_case_amount(_plan()) == "48.147456"
    assert config.fingerprint == config.fingerprint


def test_run_config_rejects_budget_below_proved_worst_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="最坏情况"):
        _config(tmp_path, amount="48")


def test_run_config_rejects_private_root_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ValueError, match="源码树内"):
        build_swebench_agent_run_config(
            _plan(),
            source_root=source,
            work_root=source / "runs",
            cache_root=tmp_path / "cache",
            git_executable=Path("/usr/bin/git"),
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
