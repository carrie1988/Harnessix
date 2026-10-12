from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

import harnessix.evals.swebench.execution as execution
from harnessix.agent.models import Budget, Turn, TurnStatus, Usage
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.execution_io import read_suite_execution_state
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.run_config import build_swebench_agent_run_config

NOW = datetime(2026, 10, 12, 2, tzinfo=UTC)


def _plan():
    rows = tuple(
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
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
        salt="harnessix-swebench-execution-test-v1",
    )


def _config(tmp_path: Path, plan):
    source = tmp_path / "source"
    source.mkdir()
    return build_swebench_agent_run_config(
        plan,
        source_root=source,
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
        git_executable=Path("/usr/bin/git"),
        fee_stop_amount="80",
        observed_at=NOW,
    )


def _turn(index: int) -> Turn:
    return Turn(
        turn_id=UUID(int=1000 + index),
        request_id=f"request-{index}",
        request_fingerprint="a" * 64,
        status=TurnStatus.COMPLETED,
        budget=Budget(),
        usage=Usage(input_tokens=100 + index, output_tokens=10),
        created_at=NOW + timedelta(minutes=index),
        completed_at=NOW + timedelta(minutes=index, seconds=30),
    )


@pytest.mark.asyncio
async def test_suite_freezes_all_25_cases_and_reopens_same_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    turns = {case.instance_id: _turn(index) for index, case in enumerate(plan.cases)}
    calls: list[str] = []

    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)

    def materialize(case, **_kwargs):
        workspace = Path(config.work_root) / "workspaces" / case.instance_id
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

    async def open_turn(_plan, _config, case, *_args):
        calls.append(case.instance_id)
        index = plan.cases.index(case)
        return UUID(int=500 + index), turns[case.instance_id]

    monkeypatch.setattr(execution, "materialize_swebench_workspace", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", open_turn)
    monkeypatch.setattr(
        execution,
        "_turn_cost",
        lambda turn, _config: (1, turn.turn_id != UUID(int=1003)),
    )
    monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: "")

    first = await execution.run_swebench_agent_suite(
        plan,
        config,
        api_key="secret",
        allow_repository_network=True,
        allow_provider_network=True,
    )
    second = await execution.run_swebench_agent_suite(
        plan,
        config,
        api_key="secret",
        allow_repository_network=False,
        allow_provider_network=True,
    )

    predictions = read_predictions(Path(config.work_root) / "predictions.jsonl", plan)
    suite = read_suite_execution_state(Path(config.work_root) / "suite-state.json", plan)
    assert first["completed_cases"] == second["completed_cases"] == 25
    assert first["unaccounted_cost_cases"] == second["unaccounted_cost_cases"] == 1
    assert first["reserved_cost_amount"] == second["reserved_cost_amount"] == "3.147456"
    assert len(predictions) == 25 and all(item.model_patch == "" for item in predictions)
    assert suite.status == "completed"
    assert calls == [case.instance_id for case in plan.cases] * 2
