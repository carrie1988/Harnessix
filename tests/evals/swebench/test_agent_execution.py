from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    Thread,
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import ApprovalOutcome, EffectClass, utc_now
from harnessix.evals.swebench.agent_execution import (
    _approval_decision,
    _request_turn,
    _require_agent_action_gateway,
    _turn_budget,
    build_swebench_agent_prompt,
)
from harnessix.evals.swebench.public_test_profile import build_swebench_case_process_profile
from harnessix.evals.swebench.selection import project_swebench_public_cases
from harnessix.product_config.process_action import process_profile_descriptor
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from tests.evals.swebench.test_predictions import build_test_plan

SHA256 = "a" * 64


def _turn(request_id: str, *items: Item, status: TurnStatus = TurnStatus.COMPLETED) -> Turn:
    now = utc_now()
    return Turn(
        turn_id=uuid4(),
        request_id=request_id,
        request_fingerprint=SHA256,
        status=status,
        budget=Budget(),
        items=items,
        created_at=now,
        completed_at=now if status in {TurnStatus.COMPLETED, TurnStatus.FAILED} else None,
    )


def _thread(*turns: Turn) -> Thread:
    now = utc_now()
    return Thread(
        thread_id=uuid4(),
        workspace="/workspace",
        turns=turns,
        created_at=now,
        updated_at=now,
    )


def _approval_fixture(
    *,
    tool: str = WORKSPACE_PATCH_TOOL,
    tool_version: str = "harnessix.workspace-patch/v2",
    presentation: Literal["tool", "patch_batch", "process"] = "patch_batch",
    arguments: dict[str, object],
) -> tuple[Turn, TrustedActionApprovalRequestContent]:
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="swebench-patch-call",
        tool=tool,
        tool_version=tool_version,
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        arguments=arguments,
        requires_approval=True,
    )
    approval = TrustedActionApprovalRequestContent(
        approval_id=uuid4(),
        call_id=call.call_id,
        presentation=presentation,
        plan_id=uuid4(),
        plan_fingerprint="1" * 64,
        execution_fingerprint="2" * 64,
        request_fingerprint="3" * 64,
        policy_id="swebench.test-policy",
        policy_version="1",
        diff_artifact=(
            ArtifactRef(
                artifact_id=uuid4(),
                sha256="4" * 64,
                size_bytes=1,
                records=1,
                complete=True,
                expires_at=utc_now() + timedelta(hours=1),
            )
            if presentation == "patch_batch"
            else None
        ),
    )
    items = (
        Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=call),
        Item(item_id=uuid4(), status=ItemStatus.STARTED, content=approval),
    )
    return _turn("swebench-request", *items, status=TurnStatus.WAITING_APPROVAL), approval


def _valid_patch_arguments() -> dict[str, object]:
    return {
        "spec_version": "harnessix.workspace-patch-input/v2",
        "files": [
            {
                "operation": "replace",
                "path": "src/example.py",
                "expected_sha256": SHA256,
                "content": "fixed = True\n",
                "mode": 420,
            }
        ],
    }


def _process_profile(tmp_path: Path):
    engine = tmp_path / "docker"
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o700)
    return build_swebench_case_process_profile(
        "owner__repo-42",
        repo="owner/repo",
        container_engine=engine,
        image_id="sha256:" + "a" * 64,
    ).profile


def test_prompt_uses_only_projected_public_case_material() -> None:
    hidden_materials = {
        "patch": "SECRET_GOLD_PATCH",
        "test_patch": "SECRET_GOLD_TEST_PATCH",
        "FAIL_TO_PASS": "SECRET_FAIL_TO_PASS",
        "PASS_TO_PASS": "SECRET_PASS_TO_PASS",
        "hints_text": "SECRET_EVALUATOR_HINT",
    }
    source_row = {
        "instance_id": "owner__repo-42",
        "repo": "owner/repo",
        "base_commit": "b" * 40,
        "problem_statement": "Fix the public regression.",
        "version": "1.2.3",
        **hidden_materials,
    }
    case = project_swebench_public_cases((source_row,))[0]

    prompt = build_swebench_agent_prompt(case)

    assert prompt == (
        "You are solving one SWE-bench task in the current repository.\n"
        "Work autonomously and leave the working tree with the smallest correct patch. Start from "
        "the named symbols and files; use focused grep/read calls rather than broad exploration. "
        "After at most four source-inspection calls, either submit the best justified "
        "patch or stop with a precise blocker. For existing files use exact edit operations with "
        "unique old_text/new_text anchors; never replace an entire file with an excerpt. After "
        "patching, select focused relative test targets or repository-native test labels that "
        "exist in the public repository and run the public-tests profile. If a test fails, "
        "diagnose the failure, repair the patch, "
        "and rerun the public-tests profile; the latest patched state must be retested before "
        "finishing. Review the final git status and diff. Do not ask questions or use network "
        "access, hidden tests, reference patches, hints, or evaluator artifacts.\n\n"
        "Instance: owner__repo-42\n"
        "Repository: owner/repo\n"
        f"Base commit: {'b' * 40}\n\n"
        "Problem statement:\n"
        "Fix the public regression."
    )
    assert all(secret not in prompt for secret in hidden_materials.values())


def test_turn_budget_exactly_matches_preregistered_task_budget() -> None:
    plan = build_test_plan()

    runtime_budget = _turn_budget(plan)

    assert runtime_budget.model_dump(mode="python") == plan.task_budget.model_dump(mode="python")


def test_valid_workspace_patch_batch_is_approved(tmp_path: Path) -> None:
    turn, approval = _approval_fixture(arguments=_valid_patch_arguments())

    decision = _approval_decision(turn, approval, _process_profile(tmp_path))

    assert decision.outcome is ApprovalOutcome.APPROVED
    assert decision.actor == "harnessix-swebench-runner"
    assert decision.reason == "SWE-bench隔离Workspace内的受控Patch或公开测试Profile"


def test_current_public_test_profile_with_bounded_selector_is_approved(tmp_path: Path) -> None:
    profile = _process_profile(tmp_path)
    descriptor = process_profile_descriptor(profile)
    turn, approval = _approval_fixture(
        tool=descriptor.name,
        tool_version=descriptor.version,
        presentation="process",
        arguments={
            "profile": "public-tests",
            "selectors": ["tests/test_regression.py::test_public_behavior"],
        },
    )

    decision = _approval_decision(turn, approval, profile)

    assert decision.outcome is ApprovalOutcome.APPROVED


@pytest.mark.parametrize(
    ("tool", "presentation", "arguments"),
    [
        pytest.param(
            WORKSPACE_PATCH_TOOL,
            "patch_batch",
            {"spec_version": "harnessix.workspace-patch-input/v2", "files": []},
            id="invalid-patch-arguments",
        ),
        pytest.param(
            "run_arbitrary_command",
            "patch_batch",
            _valid_patch_arguments(),
            id="different-tool",
        ),
        pytest.param(
            WORKSPACE_PATCH_TOOL,
            "tool",
            _valid_patch_arguments(),
            id="non-patch-presentation",
        ),
    ],
)
def test_approval_rejects_everything_outside_the_exact_patch_contract(
    tool: str,
    presentation: Literal["tool", "patch_batch", "process"],
    arguments: dict[str, object],
    tmp_path: Path,
) -> None:
    turn, approval = _approval_fixture(
        tool=tool,
        presentation=presentation,
        arguments=arguments,
    )

    decision = _approval_decision(turn, approval, _process_profile(tmp_path))

    assert decision.outcome is ApprovalOutcome.REJECTED
    assert decision.actor == "harnessix-swebench-runner"
    assert decision.reason == "SWE-bench只批准正式Workspace Patch或当前公开测试Profile"


@pytest.mark.parametrize(
    ("tool_version", "arguments"),
    [
        pytest.param(
            "harnessix.product-process/v1:wrong",
            {"profile": "public-tests", "selectors": ["tests/test_public.py"]},
            id="wrong-profile-version",
        ),
        pytest.param(
            None,
            {"profile": "public-tests", "selectors": ["../hidden/test_secret.py"]},
            id="unsafe-selector",
        ),
        pytest.param(
            None,
            {"profile": "other", "selectors": ["tests/test_public.py"]},
            id="wrong-profile-id",
        ),
    ],
)
def test_profile_approval_rejects_nonexact_or_invalid_calls(
    tmp_path: Path,
    tool_version: str | None,
    arguments: dict[str, object],
) -> None:
    profile = _process_profile(tmp_path)
    descriptor = process_profile_descriptor(profile)
    turn, approval = _approval_fixture(
        tool=descriptor.name,
        tool_version=tool_version or descriptor.version,
        presentation="process",
        arguments=arguments,
    )

    decision = _approval_decision(turn, approval, profile)

    assert decision.outcome is ApprovalOutcome.REJECTED


def test_workspace_patch_approval_rejects_nonexact_tool_version(tmp_path: Path) -> None:
    turn, approval = _approval_fixture(
        tool_version="harnessix.workspace-patch/v999",
        arguments=_valid_patch_arguments(),
    )

    decision = _approval_decision(turn, approval, _process_profile(tmp_path))

    assert decision.outcome is ApprovalOutcome.REJECTED


@pytest.mark.parametrize(
    "names",
    [
        pytest.param((WORKSPACE_PATCH_TOOL,), id="profile-omitted"),
        pytest.param(("run_profile.public-tests",), id="patch-omitted"),
        pytest.param(
            (WORKSPACE_PATCH_TOOL, "run_profile.public-tests", "run_profile.other"),
            id="extra-profile",
        ),
    ],
)
def test_agent_action_capability_omission_fails_closed_before_model_use(
    tmp_path: Path,
    names: tuple[str, ...],
) -> None:
    profile = _process_profile(tmp_path)
    profile_descriptor = process_profile_descriptor(profile)

    class Gateway:
        def definitions(self):
            return tuple(
                SimpleNamespace(
                    name=name,
                    version=(
                        profile_descriptor.version
                        if name == profile_descriptor.name
                        else "harnessix.workspace-patch/v2"
                    ),
                )
                for name in names
            )

    gateway = cast(RouterBackedAgentActionGateway, Gateway())

    with pytest.raises(KernelError) as caught:
        _require_agent_action_gateway(gateway, profile)

    assert caught.value.code == "swebench_action_unavailable"


def test_agent_action_gateway_accepts_patch_and_current_exact_profile(tmp_path: Path) -> None:
    profile = _process_profile(tmp_path)
    descriptor = process_profile_descriptor(profile)

    class Gateway:
        def definitions(self):
            return (
                SimpleNamespace(name=WORKSPACE_PATCH_TOOL, version="harnessix.workspace-patch/v2"),
                SimpleNamespace(name=descriptor.name, version=descriptor.version),
            )

    gateway = cast(RouterBackedAgentActionGateway, Gateway())

    assert _require_agent_action_gateway(gateway, profile) is gateway


@pytest.mark.parametrize(
    "turn_request_ids",
    [
        pytest.param(("planned", "planned"), id="duplicate-request"),
        pytest.param(("unexpected",), id="only-unplanned-turn"),
        pytest.param(("planned", "unexpected"), id="planned-plus-unplanned-turn"),
    ],
)
def test_request_projection_fails_closed_for_duplicate_or_unplanned_turns(
    turn_request_ids: tuple[str, ...],
) -> None:
    thread = _thread(*(_turn(request_id) for request_id in turn_request_ids))

    with pytest.raises(KernelError) as caught:
        _request_turn(thread, "planned")

    assert caught.value.code == "swebench_run_projection_invalid"
