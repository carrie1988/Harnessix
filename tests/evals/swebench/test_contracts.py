from __future__ import annotations

import pytest
from pydantic import ValidationError

from harnessix.evals.swebench import (
    SWE_BENCH_SELECTION_POLICY,
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchPreRegistrationPlan,
    SweBenchPublicCase,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.contracts import (
    swebench_candidate_manifest_digest,
    swebench_preregistration_plan_digest,
)
from harnessix.evals.swebench.selection import project_swebench_public_cases


def dataset_rows(count: int = 40) -> list[dict[str, object]]:
    return [
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix public issue {index}",
            "version": f"{index % 3}.0",
            "issue_id": index + 100,
            "issue_url": f"https://github.com/owner{index % 5}/repo{index % 5}/issues/{index}",
            "pr_url": f"https://github.com/owner{index % 5}/repo{index % 5}/pull/{index}",
            "created_at": "2024-01-02T03:04:05Z",
            "patch": f"gold-{index}",
            "test_patch": f"tests-{index}",
            "FAIL_TO_PASS": f'["failed-{index}"]',
            "PASS_TO_PASS": f'["passed-{index}"]',
            "hints_text": f"private hint {index}",
        }
        for index in range(count)
    ]


def model_config() -> SweBenchModelConfig:
    return SweBenchModelConfig(
        provider="openai_chat",
        parameters=(
            SweBenchModelParameter(name="reasoning_effort", value="high"),
            SweBenchModelParameter(name="temperature", value=0.0),
        ),
    )


def task_budget() -> SweBenchTaskBudget:
    return SweBenchTaskBudget(
        max_steps=32,
        max_tokens=50_000,
        timeout_seconds=1_800.0,
        max_output_chars=65_536,
        max_tool_calls_per_step=16,
    )


def plan(rows: list[dict[str, object]] | None = None) -> SweBenchPreRegistrationPlan:
    return build_swebench_preregistration_plan(
        dataset_rows() if rows is None else rows,
        dataset_name="SWE-bench/SWE-bench_Verified",
        dataset_revision="1" * 40,
        dataset_artifact_sha256="a" * 64,
        split="test",
        harnessix_revision="2" * 40,
        swebench_revision="3" * 40,
        swebench_package_version="5.0.2",
        task_repo_revision="4" * 40,
        model_name="example/model-v1",
        model_config=model_config(),
        task_budget=task_budget(),
        salt="public-preregistration-salt-v1",
    )


@pytest.mark.parametrize(
    "field",
    [
        "patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "hints_text",
        "environment_setup_commit",
        "difficulty",
    ],
)
def test_evaluator_only_fields_cannot_enter_public_contract(field: str) -> None:
    public = {name: dataset_rows(1)[0][name] for name in SweBenchPublicCase.model_fields}

    with pytest.raises(ValidationError):
        SweBenchPublicCase.model_validate({**public, field: "leaked"})


def test_plan_binds_scope_budget_model_cases_and_summary() -> None:
    current = plan()

    assert current.dataset_name == "SWE-bench/SWE-bench_Verified"
    assert current.dataset_revision == "1" * 40
    assert current.dataset_artifact_sha256 == "a" * 64
    assert current.candidate_count == 40
    assert current.candidate_manifest_sha256 == swebench_candidate_manifest_digest(
        project_swebench_public_cases(dataset_rows())
    )
    assert current.split == "test"
    assert current.harnessix_revision == "2" * 40
    assert current.swebench_revision == "3" * 40
    assert current.swebench_package_version == "5.0.2"
    assert current.task_repo_revision == "4" * 40
    assert current.model_name == "example/model-v1"
    assert current.model_settings == model_config()
    assert current.task_budget == task_budget()
    assert current.trials_per_case == 1
    assert current.agent_network_mode == "none"
    assert current.model_retry_policy == "none"
    assert current.selection_policy == SWE_BENCH_SELECTION_POLICY
    assert len(current.cases) == 25
    assert current.plan_sha256 == swebench_preregistration_plan_digest(current)
    assert all(
        not {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text"}
        & set(case.model_dump())
        for case in current.cases
    )


def test_plan_rejects_summary_tampering() -> None:
    payload = plan().model_dump(mode="python")
    payload["plan_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="digest"):
        SweBenchPreRegistrationPlan.model_validate(payload)


def test_plan_rejects_noncanonical_case_order_even_with_recomputed_summary() -> None:
    current = plan()
    reordered_cases = (current.cases[1], current.cases[0], *current.cases[2:])
    candidate = current.model_copy(
        update={"cases": reordered_cases, "plan_sha256": "0" * 64}
    )
    payload = candidate.model_dump(mode="python")
    payload["plan_sha256"] = swebench_preregistration_plan_digest(candidate)

    with pytest.raises(ValidationError, match="canonical"):
        SweBenchPreRegistrationPlan.model_validate(payload)


def test_contracts_are_strict_forbid_extras_and_are_frozen() -> None:
    with pytest.raises(ValidationError):
        SweBenchTaskBudget(max_steps="32")
    with pytest.raises(ValidationError):
        SweBenchTaskBudget(unexpected=True)

    current = plan()
    with pytest.raises(ValidationError):
        current.salt = "changed"  # type: ignore[misc]
