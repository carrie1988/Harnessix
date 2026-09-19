from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.evals.task_pack import (
    build_task_pack_product_profile,
    builtin_coding_eval_task_pack,
)
from harnessix.evals.task_pack_materializer import materialize_task_pack_case
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.secrets.provider import SecretMaterial
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer

_PROJECT_ROOT = Path(__file__).parents[2]
_ENGINEERING_SOLUTIONS = _PROJECT_ROOT / "benchmarks/taskpacks/harnessix-engineering-v1/solutions"


class _NoSecrets:
    def resolve(self, name: str) -> SecretMaterial:
        raise AssertionError(f"无Secret Profile不应解析{name}")


async def _git_output(workspace: Path, *arguments: str) -> str:
    git = shutil.which("git") or "git"
    body = await asyncio.to_thread(
        subprocess.check_output,
        (git, *arguments),
        cwd=workspace,
        text=True,
    )
    return body.strip()


def _tool_call(profile_id: str, suffix: str):
    return [
        ResponseStarted(response_id=f"task-pack-response-{suffix}"),
        ToolCallCompleted(
            call_id=f"task-pack-call-{suffix}",
            tool=f"run_profile.{profile_id}",
            arguments={"profile": profile_id, "selectors": []},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def _approved_profile_turn(
    agent: AgentRuntime,
    thread_id,
    request_id: str,
) -> ToolResultContent:
    waiting = await agent.run_turn(thread_id, "运行固定Task Pack检查", request_id=request_id)
    approval = next(
        item.content
        for item in waiting.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    )
    assert waiting.status is TurnStatus.WAITING_APPROVAL
    await agent.reply_approval(
        thread_id,
        waiting.turn_id,
        approval.approval_id,
        fingerprint=approval.request_fingerprint,
        decision=ApprovalDecision(
            outcome=ApprovalOutcome.APPROVED,
            actor="task-pack-integration",
        ),
    )
    completed = await agent.resume_turn(thread_id, waiting.turn_id)
    assert completed.status is TurnStatus.COMPLETED
    return next(
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    )


@pytest.mark.parametrize(
    ("case_id", "image_environment", "old", "new"),
    [
        (
            "javascript-slug-lowercase",
            "HARNESSIX_TEST_NODE_IMAGE",
            'value.trim().replace(/\\s+/g, "-")',
            'value.trim().toLowerCase().replace(/\\s+/g, "-")',
        ),
        (
            "python-mathbox-addition",
            "HARNESSIX_TEST_PYTHON_IMAGE",
            "left - right",
            "left + right",
        ),
    ],
)
async def test_builtin_task_pack_profile_fails_then_passes_through_product_runtime(
    tmp_path: Path,
    case_id: str,
    image_environment: str,
    old: str,
    new: str,
) -> None:
    image = os.environ.get(image_environment)
    docker = shutil.which("docker")
    git = shutil.which("git")
    if not image or not docker or not git or os.name != "posix":
        pytest.skip("未配置Task Pack固定镜像的真实产品Container验收")

    loaded = builtin_coding_eval_task_pack()
    case = loaded.manifest.case(case_id)
    source_profile = loaded.manifest.profile(case.profile_id)
    assert image == source_profile.image
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    materialized = materialize_task_pack_case(
        loaded,
        runs,
        Path(git),
        case_id,
        uuid4(),
    )
    profile = build_task_pack_product_profile(loaded, case.profile_id, Path(docker))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    provider = ScriptedProvider(
        [
            _tool_call(case.profile_id, "baseline"),
            answer("基线检查完成"),
            _tool_call(case.profile_id, "final"),
            answer("最终检查完成"),
        ]
    )
    sessions = SQLiteSessionStore(state / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)

    async with CodingToolRuntime(materialized.workspace, artifacts=artifacts) as tools:
        async with open_default_product_action_runtime(
            state,
            materialized.workspace,
            artifacts,
            _NoSecrets(),
            build_product_action_config(
                workspace_patch_enabled=False,
                process_profiles=(profile,),
            ),
            artifact_workspace_scope=tools.workspace_scope,
        ) as composition:
            assert composition.gateway is not None
            capability = next(
                item
                for item in composition.report.capabilities
                if item.capability_id == f"run_profile.{case.profile_id}"
            )
            assert capability.status == "verified"
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
            ) as agent:
                thread = await agent.create_thread(str(materialized.workspace))
                baseline = await _approved_profile_turn(
                    agent,
                    thread.thread_id,
                    f"task-pack-{case_id}-baseline",
                )
                assert baseline.outcome == "failed"
                assert baseline.error is not None
                assert baseline.error.code == "process_nonzero_exit"
                assert isinstance(baseline.output, dict)
                baseline_ref = baseline.output["artifact"]
                assert isinstance(baseline_ref, dict)
                baseline_page = await artifacts.read(
                    thread.thread_id,
                    tools.workspace_scope,
                    str(baseline_ref["artifact_id"]),
                    limit=200,
                )
                baseline_document = parse_trusted_process_output(baseline_page.text.encode())
                assert baseline_document.summary.returncode not in {None, 0}

                changed = materialized.workspace / case.task.allowed_changed_paths[0]
                original = changed.read_text(encoding="utf-8")
                assert old in original
                changed.write_text(original.replace(old, new), encoding="utf-8")

                final = await _approved_profile_turn(
                    agent,
                    thread.thread_id,
                    f"task-pack-{case_id}-final",
                )
                assert final.outcome == "succeeded" and isinstance(final.output, dict)
                final_ref = final.output["artifact"]
                assert isinstance(final_ref, dict)
                final_page = await artifacts.read(
                    thread.thread_id,
                    tools.workspace_scope,
                    str(final_ref["artifact_id"]),
                    limit=200,
                )
                final_document = parse_trusted_process_output(final_page.text.encode())
                assert final_document.summary.returncode == 0

    status = await _git_output(materialized.workspace, "status", "--short")
    assert status == f"M {case.task.allowed_changed_paths[0]}"
    head = await _git_output(materialized.workspace, "rev-parse", "HEAD")
    repository = loaded.manifest.repository(case.repository_id)
    assert head == repository.repository.source_revision


@pytest.mark.parametrize(
    "case_id",
    [
        "agents-dump-compatible-refactor",
        "agents-normalize-tool-name",
        "agents-payload-bytes-test",
        "agents-secret-redaction-review",
        "langchain-batch-none-test",
        "langchain-storage-replacement",
        "langchain-stringify-dict-keys",
        "opencode-path-normalization",
        "opencode-retry-delay-refactor",
        "opencode-terminal-url-review",
    ],
)
async def test_engineering_task_pack_profiles_fail_then_pass_through_product_runtime(
    tmp_path: Path,
    case_id: str,
) -> None:
    docker = shutil.which("docker")
    git = shutil.which("git")
    if not docker or not git or os.name != "posix":
        pytest.skip("未配置Task Pack真实产品Container验收")

    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 1)
    case = loaded.manifest.case(case_id)
    source_profile = loaded.manifest.profile(case.profile_id)
    image_environment = (
        "HARNESSIX_TEST_PYTHON_IMAGE"
        if source_profile.language == "python"
        else "HARNESSIX_TEST_NODE_IMAGE"
    )
    if os.environ.get(image_environment) != source_profile.image:
        pytest.skip("未配置Task Pack固定镜像")

    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    materialized = materialize_task_pack_case(
        loaded,
        runs,
        Path(git),
        case_id,
        uuid4(),
    )
    profile = build_task_pack_product_profile(loaded, case.profile_id, Path(docker))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    provider = ScriptedProvider(
        [
            _tool_call(case.profile_id, "baseline"),
            answer("基线检查完成"),
            _tool_call(case.profile_id, "final"),
            answer("最终检查完成"),
        ]
    )
    sessions = SQLiteSessionStore(state / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)

    async with CodingToolRuntime(materialized.workspace, artifacts=artifacts) as tools:
        async with open_default_product_action_runtime(
            state,
            materialized.workspace,
            artifacts,
            _NoSecrets(),
            build_product_action_config(
                workspace_patch_enabled=False,
                process_profiles=(profile,),
            ),
            artifact_workspace_scope=tools.workspace_scope,
        ) as composition:
            assert composition.gateway is not None
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
            ) as agent:
                thread = await agent.create_thread(str(materialized.workspace))
                baseline = await _approved_profile_turn(
                    agent,
                    thread.thread_id,
                    f"task-pack-{case_id}-baseline",
                )
                assert baseline.outcome == "failed"
                assert baseline.error is not None
                assert baseline.error.code == "process_nonzero_exit"

                applied = await asyncio.to_thread(
                    subprocess.run,
                    (
                        git,
                        "apply",
                        "--unidiff-zero",
                        "--whitespace=nowarn",
                        str(_ENGINEERING_SOLUTIONS / f"{case_id}.patch"),
                    ),
                    cwd=materialized.workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=30,
                )
                assert applied.returncode == 0

                final = await _approved_profile_turn(
                    agent,
                    thread.thread_id,
                    f"task-pack-{case_id}-final",
                )
                assert final.outcome == "succeeded"

    status = await _git_output(
        materialized.workspace,
        "status",
        "--short",
        "--untracked-files=all",
    )
    assert status.strip().split(maxsplit=1)[1] == case.task.allowed_changed_paths[0]
