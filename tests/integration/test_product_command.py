from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.action_runtime import open_default_product_action_runtime
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.product_config.test_process_action import _NoSecrets


class _ThreadScriptedProvider:
    """每个Thread拥有独立脚本，避免新Turn的step重置复用其他任务提案。"""

    def __init__(self) -> None:
        self.providers: dict[UUID, ScriptedProvider] = {}

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncIterator[ProviderEvent]:
        async for event in self.providers[request.thread_id].stream(request, cancel):
            yield event


@pytest.mark.parametrize("persistent_dependencies", [False, True])
async def test_real_product_command_runs_dynamic_commands_and_preserves_read_only_workspace(
    tmp_path: Path, persistent_dependencies: bool
) -> None:
    image = os.environ.get("HARNESSIX_TEST_CONTAINER_IMAGE")
    docker = shutil.which("docker")
    if not image or not docker or os.name != "posix":
        pytest.skip("未配置固定摘要的真实产品Container命令验收")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    source = workspace / "input.txt"
    source.write_text("original-source\n", encoding="utf-8")
    source.chmod(0o644)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    profile = build_product_process_profile(
        profile_id="workspace-command",
        version="2026.10.1",
        description="运行批准的动态命令",
        container_engine=docker,
        image=image,
        program="/bin/sh",
        arguments=("-c",),
        selector_policy="shell_command",
        persistent_dependencies=persistent_dependencies,
        timeout_seconds=30,
        max_output_bytes=64 * 1024,
        cpu_limit=0.5,
        memory_bytes=128 * 1024 * 1024,
        process_limit=32,
    )
    cases = (
        ("python -c 'print(6 * 8)'", 0, b"48\n"),
        ("python -c 'print(sum(range(5)))'", 0, b"10\n"),
        ("printf 'alpha\\nbeta\\n' | sed -n '2p'", 0, b"beta\n"),
        ("printf 'before-nonzero\\n'; exit 7", 7, b"before-nonzero\n"),
        (
            'python -c "from pathlib import Path; '
            "Path('/workspace/input.txt').write_text('overwritten')\"",
            1,
            b"",
        ),
    )
    if persistent_dependencies:
        cases = (
            (
                "printf 'thread-one-marker\\n' > \"$HARNESSIX_DEPENDENCIES/marker.txt\" && "
                "printf 'dependency-written\\n'",
                0,
                b"dependency-written\n",
            ),
            ("cat /harnessix/deps/marker.txt", 0, b"thread-one-marker\n"),
            *cases,
        )
    scripts = []
    for index, (cmd, _, _) in enumerate(cases):
        scripts.append(
            [
                ResponseStarted(response_id=f"command-response-{index}"),
                ToolCallCompleted(
                    call_id=f"command-call-{index}",
                    tool="exec_command.workspace-command",
                    arguments={"profile": profile.profile_id, "cmd": cmd},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ]
        )
    scripts.append(answer("命令执行完成"))
    # 仅模型提案由脚本提供；容器执行、人工批准和输出Artifact使用真实产品实现。
    provider = _ThreadScriptedProvider()
    sessions = SQLiteSessionStore(state / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
        async with open_default_product_action_runtime(
            state,
            workspace,
            artifacts,
            _NoSecrets(),
            build_product_action_config(workspace_patch_enabled=False, process_profiles=(profile,)),
            artifact_workspace_scope=tools.workspace_scope,
        ) as composition:
            assert composition.gateway is not None
            capability = next(
                item
                for item in composition.report.capabilities
                if item.capability_id == "exec_command.workspace-command"
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
                provider.providers[thread.thread_id] = ScriptedProvider(scripts)
                plan_ids = set()
                waiting = await agent.run_turn(
                    thread.thread_id, "依次运行动态命令验收", request_id="dynamic-commands"
                )
                for index, (cmd, returncode, expected_stdout) in enumerate(cases):
                    assert waiting.status is TurnStatus.WAITING_APPROVAL
                    pending = [
                        item.content
                        for item in waiting.items
                        if isinstance(item.content, TrustedActionApprovalRequestContent)
                        and item.content.decision is None
                        and item.content.route_state == "pending_approval"
                        and item.content.plan_id not in plan_ids
                    ]
                    assert len(pending) == 1
                    approval = pending[0]
                    assert approval.presentation == "process"
                    call = next(
                        item.content
                        for item in waiting.items
                        if isinstance(item.content, ToolCallContent)
                        and item.content.call_id == approval.call_id
                    )
                    assert call.provider_call_id == f"command-call-{index}"
                    assert call.tool == "exec_command.workspace-command"
                    assert call.arguments == {"profile": profile.profile_id, "cmd": cmd}
                    assert approval.plan_id not in plan_ids
                    plan_ids.add(approval.plan_id)
                    assert not any(
                        isinstance(item.content, ToolResultContent)
                        and item.content.call_id == approval.call_id
                        for item in waiting.items
                    )
                    await agent.reply_approval(
                        thread.thread_id,
                        waiting.turn_id,
                        approval.approval_id,
                        fingerprint=approval.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="container-reviewer"
                        ),
                    )
                    progressed = await agent.resume_turn(thread.thread_id, waiting.turn_id)
                    assert progressed.status is (
                        TurnStatus.COMPLETED
                        if index == len(cases) - 1
                        else TurnStatus.WAITING_APPROVAL
                    )
                    result = next(
                        item.content
                        for item in progressed.items
                        if isinstance(item.content, ToolResultContent)
                        and item.content.call_id == approval.call_id
                    )
                    assert result.outcome == ("succeeded" if returncode == 0 else "failed")
                    if returncode:
                        assert result.error is not None
                        assert result.error.code == "process_nonzero_exit"
                    assert isinstance(result.output, dict)
                    assert result.output["returncode"] == returncode
                    assert result.output["process_id"] == str(approval.plan_id)
                    reference = cast(dict[str, Any], result.output["artifact"])
                    page = await artifacts.read(
                        thread.thread_id,
                        tools.workspace_scope,
                        reference["artifact_id"],
                        limit=200,
                    )
                    document = parse_trusted_process_output(page.text.encode())
                    streams = {
                        name: b"".join(
                            chunk.data() for chunk in document.chunks if chunk.stream == name
                        )
                        for name in ("stdout", "stderr")
                    }
                    assert document.summary.complete
                    assert streams["stdout"] == expected_stdout
                    if index == len(cases) - 1:
                        assert b"Read-only file system" in streams["stderr"]
                        assert (
                            await agent.resume_turn(thread.thread_id, waiting.turn_id) == progressed
                        )
                    assert source.read_text(encoding="utf-8") == "original-source\n"
                    waiting = progressed

                if persistent_dependencies:
                    isolated_thread = await agent.create_thread(str(workspace))
                    isolated_cmd = (
                        "test -d /harnessix/deps && test ! -e /harnessix/deps/marker.txt "
                        "&& printf 'thread-isolated\\n'"
                    )
                    provider.providers[isolated_thread.thread_id] = ScriptedProvider(
                        [
                            [
                                ResponseStarted(response_id="isolated-response"),
                                ToolCallCompleted(
                                    call_id="isolated-call",
                                    tool="exec_command.workspace-command",
                                    arguments={"profile": profile.profile_id, "cmd": isolated_cmd},
                                ),
                                ResponseCompleted(finish_reason="tool_calls"),
                            ],
                            answer("任务依赖隔离验收完成"),
                        ]
                    )
                    isolated = await agent.run_turn(
                        isolated_thread.thread_id,
                        "验证同工作区的新任务不能读取原任务依赖",
                        request_id="isolated-dependencies",
                    )
                    assert isolated.status is TurnStatus.WAITING_APPROVAL
                    approval = next(
                        item.content
                        for item in isolated.items
                        if isinstance(item.content, TrustedActionApprovalRequestContent)
                        and item.content.decision is None
                    )
                    assert approval.plan_id not in plan_ids
                    await agent.reply_approval(
                        isolated_thread.thread_id,
                        isolated.turn_id,
                        approval.approval_id,
                        fingerprint=approval.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="container-reviewer"
                        ),
                    )
                    completed = await agent.resume_turn(isolated_thread.thread_id, isolated.turn_id)
                    assert completed.status is TurnStatus.COMPLETED
                    result = next(
                        item.content
                        for item in completed.items
                        if isinstance(item.content, ToolResultContent)
                        and item.content.call_id == approval.call_id
                    )
                    assert result.outcome == "succeeded" and isinstance(result.output, dict)
                    reference = cast(dict[str, Any], result.output["artifact"])
                    page = await artifacts.read(
                        isolated_thread.thread_id,
                        tools.workspace_scope,
                        reference["artifact_id"],
                        limit=200,
                    )
                    document = parse_trusted_process_output(page.text.encode())
                    assert document.summary.complete
                    assert (
                        b"".join(chunk.data() for chunk in document.chunks) == b"thread-isolated\n"
                    )
                    assert source.read_text(encoding="utf-8") == "original-source\n"
