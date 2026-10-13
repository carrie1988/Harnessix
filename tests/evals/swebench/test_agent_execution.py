from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
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
    _PROMPT_VERSION,
    _approval_decision,
    _drive_turn,
    _request_turn,
    _require_agent_action_gateway,
    _turn_budget,
    build_swebench_agent_prompt,
    run_swebench_agent_case,
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
        "Batch independent source reads in one response when supported; write and test in order. "
        "Locate the exact named definition before editing, rather than a similarly named method. "
        "For a known file prefer read_file with a unique literal anchor and max_lines=60; this "
        "returns nearby source and full content_sha256 without separate grep/read steps. "
        "Use focused 40-60 line windows with max_utf8_bytes=4096; expand only for missing "
        "evidence. Use exact literal grep queries, narrow include/path and max_results=10. "
        "Reserve repeated input for validation, repair and final review. "
        "For existing files use exact edit operations with "
        "unique old_text/new_text anchors; never replace an entire file with an excerpt. After "
        "patching, select focused relative test targets or repository-native test labels that "
        "you have actually observed in the public repository. Never guess an exact test node; "
        "when only the file is known, run that file. Then run the public-tests profile. If a test "
        "fails, "
        "diagnose the failure, repair the patch, "
        "and rerun the public-tests profile; the latest patched state must be retested before "
        "finishing. After the final test succeeds, inspect the forced git_review status and diff "
        "evidence. Do not ask questions or use network "
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


@pytest.mark.parametrize("tokens,steps,seconds", [(50_000, 16, 900), (12_345, 7, 321)])
def test_prompt_exposes_exact_budget_without_changing_problem_or_task_limits(
    tokens: int, steps: int, seconds: int
) -> None:
    case = build_test_plan().cases[0]
    budget = Budget(max_tokens=tokens, max_steps=steps, timeout_seconds=seconds)
    frozen = budget.model_dump_json()
    prompt = build_swebench_agent_prompt(case, budget)
    assert f"{tokens} cumulative input-plus-output tokens" in prompt
    assert f"{steps} model steps, {seconds} seconds" in prompt
    assert "not a per-request token allowance" in prompt
    assert prompt.endswith("Problem statement:\n" + case.problem_statement)
    assert budget.model_dump_json() == frozen


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    ["none", "thread_missing", "thread_drift", "turn_missing", "turn_drift", "nonterminal"],
)
async def test_frozen_terminal_replay_never_creates_or_resumes_an_attempt(tmp_path, defect):
    plan = build_test_plan()
    case = plan.cases[0]
    request = f"swebench:{plan.plan_sha256}:{SHA256}:{case.instance_id}:{_PROMPT_VERSION}"
    turn = _turn(request)
    thread = _thread(turn)
    expected = (thread.thread_id, turn.turn_id)
    if defect == "thread_missing":
        thread = None
    elif defect == "thread_drift":
        thread = thread.model_copy(update={"thread_id": uuid4()})
    elif defect == "turn_missing":
        thread = thread.model_copy(update={"turns": ()})
    elif defect == "turn_drift":
        thread = thread.model_copy(
            update={"turns": (turn.model_copy(update={"turn_id": uuid4()}),)}
        )
    elif defect == "nonterminal":
        thread = thread.model_copy(
            update={"turns": (turn.model_copy(update={"status": TurnStatus.ACCEPTED}),)}
        )

    class Owner:
        async def authenticated_single_thread(self, _history):
            return thread

    class Runtime:
        def __getattr__(self, name):
            pytest.fail(f"终态重放不得调用运行时操作: {name}")

    async def replay():
        return await _drive_turn(
            Runtime(),
            Owner(),
            plan,
            case,
            Path("/workspace"),
            SHA256,
            _process_profile(tmp_path),
            CancelToken(),
            expected,
        )

    if defect == "none":
        assert await replay() == (expected[0], turn)
    else:
        with pytest.raises(KernelError) as caught:
            await replay()
        assert caught.value.code == "swebench_case_evidence_mismatch"


@pytest.mark.asyncio
async def test_missing_frozen_session_stops_before_opening_provider_or_publication(
    tmp_path, monkeypatch
):
    from harnessix.evals.swebench import agent_execution
    from harnessix.models.config import OpenAIChatConfig

    def forbidden(*_args, **_kwargs):
        pytest.fail("缺失冻结Session不得打开Provider或重建发布状态")

    monkeypatch.setattr(agent_execution, "_provider", forbidden)
    monkeypatch.setattr(agent_execution, "open_task_pack_publication", forbidden)
    plan = build_test_plan()
    root = tmp_path / "missing-run"
    with pytest.raises(KernelError) as caught:
        await run_swebench_agent_case(
            plan=plan,
            case=plan.cases[0],
            run_root=root,
            workspace=tmp_path,
            git_executable=Path("/usr/bin/git"),
            provider_config=OpenAIChatConfig(model="fixture"),
            execution_fingerprint=SHA256,
            process_profile=_process_profile(tmp_path),
            api_key="offline",
            expected_terminal_turn=(uuid4(), uuid4()),
        )
    assert caught.value.code == "swebench_case_evidence_mismatch"
    assert not root.exists()


@pytest.mark.parametrize("enabled", [None, 1, "true"])
async def test_invalid_budget_window_stops_before_opening_provider(tmp_path, monkeypatch, enabled):
    from harnessix.evals.swebench import agent_execution
    from harnessix.models.config import OpenAIChatConfig

    def forbidden(*_args, **_kwargs):
        pytest.fail("非法预算窗口开关不得打开Provider或发布目录")

    monkeypatch.setattr(agent_execution, "_provider", forbidden)
    monkeypatch.setattr(agent_execution, "open_task_pack_publication", forbidden)
    plan = build_test_plan()
    root = tmp_path / "invalid-run"
    with pytest.raises(KernelError) as caught:
        await run_swebench_agent_case(
            plan=plan,
            case=plan.cases[0],
            run_root=root,
            workspace=tmp_path,
            git_executable=Path("/usr/bin/git"),
            provider_config=OpenAIChatConfig(model="fixture"),
            execution_fingerprint=SHA256,
            process_profile=_process_profile(tmp_path),
            api_key="offline",
            budget_pressure_compaction=enabled,
        )
    assert caught.value.code == "swebench_execution_config_invalid"
    assert not root.exists()
