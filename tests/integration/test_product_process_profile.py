from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.secrets.provider import SecretMaterial
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer


class _NoSecrets:
    def resolve(self, name: str) -> SecretMaterial:
        raise AssertionError(f"无Secret Profile不应解析{name}")


async def test_real_product_process_profile_runs_only_after_approval(tmp_path: Path) -> None:
    image = os.environ.get("HARNESSIX_TEST_CONTAINER_IMAGE")
    docker = shutil.which("docker")
    if not image or not docker or os.name != "posix":
        pytest.skip("未配置固定摘要的真实产品Container验收")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    source = workspace / "input.txt"
    source.write_text("workspace-content\n", encoding="utf-8")
    source.chmod(0o644)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    profile = build_product_process_profile(
        profile_id="container-smoke",
        version="2026.09.1",
        description="运行固定容器产品验收",
        container_engine=docker,
        image=image,
        program="/bin/sh",
        arguments=("-c", "cat /workspace/input.txt; printf 'profile-ok\\n'"),
        timeout_seconds=30,
        max_output_bytes=64 * 1024,
        cpu_limit=0.5,
        memory_bytes=64 * 1024 * 1024,
        process_limit=16,
    )
    action = [
        ResponseStarted(response_id="product-process-response"),
        ToolCallCompleted(
            call_id="product-process-call",
            tool="run_profile.container-smoke",
            arguments={"profile": "container-smoke", "selectors": []},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
    provider = ScriptedProvider([action, answer("容器验收完成")])
    sessions = SQLiteSessionStore(state / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)

    async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
        async with open_default_product_action_runtime(
            state,
            workspace,
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
                if item.capability_id == "run_profile.container-smoke"
            )
            assert capability.status == "verified"
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
            ) as agent:
                thread = await agent.create_thread(str(workspace))
                waiting = await agent.run_turn(
                    thread.thread_id,
                    "运行固定容器验收",
                    request_id="product-container-smoke",
                )
                approval = next(
                    item.content
                    for item in waiting.items
                    if isinstance(item.content, TrustedActionApprovalRequestContent)
                )
                assert waiting.status is TurnStatus.WAITING_APPROVAL
                await agent.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor="container-reviewer",
                    ),
                )
                completed = await agent.resume_turn(thread.thread_id, waiting.turn_id)
                result = next(
                    item.content
                    for item in completed.items
                    if isinstance(item.content, ToolResultContent)
                )
                assert completed.status is TurnStatus.COMPLETED
                assert result.outcome == "succeeded" and isinstance(result.output, dict)
                reference = result.output["artifact"]
                page = await artifacts.read(
                    thread.thread_id,
                    tools.workspace_scope,
                    reference["artifact_id"],
                    limit=200,
                )
                document = parse_trusted_process_output(page.text.encode())
                output = b"".join(item.data() for item in document.chunks)
                assert b"workspace-content" in output and b"profile-ok" in output

    assert source.read_text(encoding="utf-8") == "workspace-content\n"
