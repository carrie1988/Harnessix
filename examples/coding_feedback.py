"""离线演示失败测试→受管Patch→测试通过→Git差异反馈闭环。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    PatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.managed import PatchWorkspaces
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.output_artifact import parse_process_output_document
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.test_contracts import TestProfile
from harnessix.processes.test_profiles import RunTestsAgentBridge
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace
from harnessix.worker import ActionWorker


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("此示例需要Git")
    return Path(executable).resolve()


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def tool_result(request: ModelRequest, tool: str, index: int = -1) -> ToolResultContent:
    calls = {
        item.content.call_id: item.content.tool
        for item in request.history
        if getattr(item.content, "kind", None) == "tool_call"
    }
    results = [
        item.content
        for item in request.history
        if isinstance(item.content, ToolResultContent) and calls.get(item.content.call_id) == tool
    ]
    return results[index]


class FeedbackProvider:
    """确定性替代模型；每一步都校验上一步真实结果。"""

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        if request.step == 1:
            tool, arguments = "run_tests", {"profile": "unit"}
        elif request.step == 2:
            failed = tool_result(request, "run_tests")
            assert failed.output["passed"] is False
            tool, arguments = (
                "read_artifact",
                {
                    "artifact_id": failed.output["artifact"]["artifact_id"],
                    "offset": 0,
                    "limit": 100,
                },
            )
        elif request.step == 3:
            page = tool_result(request, "read_artifact").output
            document = parse_process_output_document(page["text"].encode())
            stderr = b"".join(chunk.data() for chunk in document.chunks if chunk.stream == "stderr")
            assert b"expected 5" in stderr
            tool, arguments = "read_file", {"path": "calc.py"}
        elif request.step == 4:
            source = tool_result(request, "read_file").output
            tool, arguments = (
                "apply_patch",
                {
                    "path": "calc.py",
                    "expected_revision": source["revision"],
                    "edits": [{"old_text": "return a - b", "new_text": "return a + b"}],
                },
            )
        elif request.step == 5:
            patch = tool_result(request, "apply_patch")
            assert patch.patch is not None and patch.patch.state == "applied"
            tool, arguments = "run_tests", {"profile": "unit"}
        elif request.step == 6:
            assert tool_result(request, "run_tests").output["passed"] is True
            tool, arguments = "git_status", {}
        elif request.step == 7:
            status = tool_result(request, "git_status").output
            assert status["entries"][0]["path"] == "calc.py"
            tool, arguments = "git_diff", {"target": "worktree", "context_lines": 1}
        else:
            diff = tool_result(request, "git_diff").output["text"]
            assert "-    return a - b" in diff and "+    return a + b" in diff
            yield ResponseStarted(response_id="feedback-complete")
            yield TextStarted(content_id="answer")
            yield TextCompleted(
                content_id="answer",
                text="已修复加法逻辑；unit测试通过，Git状态与差异已核对。",
            )
            yield ResponseCompleted()
            return
        yield ResponseStarted(response_id=f"feedback-{request.step}")
        yield ToolCallCompleted(
            call_id=f"feedback-call-{request.step}", tool=tool, arguments=arguments
        )
        yield ResponseCompleted(finish_reason="tool_calls")


def pending_process(turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in reversed(turn.items)
        if isinstance(item.content, ProcessApprovalRequestContent) and item.content.decision is None
    )


async def approve_process(runtime: AgentRuntime, thread_id, turn) -> None:
    approval = pending_process(turn)
    await runtime.reply_approval(
        thread_id,
        turn.turn_id,
        approval.approval_id,
        fingerprint=approval.request_fingerprint,
        decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="example-reviewer"),
    )


async def exercise(root: Path) -> None:
    source = root / "source"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (source / "verify.py").write_text(
        "import runpy\nns = runpy.run_path('calc.py')\n"
        "assert ns['add'](2, 3) == 5, 'expected 5'\nprint('PASS')\n",
        encoding="utf-8",
    )
    copies = PatchWorkspaces(root / "private")
    with Workspace(source) as workspace:
        copy = copies.create(workspace, ("calc.py", "verify.py"), ReadOperation())
    repository = copy.workspace.root
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Harnessix Example")
    git(repository, "config", "user.email", "example@harnessix.invalid")
    git(repository, "add", "calc.py", "verify.py")
    git(repository, "commit", "-qm", "baseline")

    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(repository, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(root / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    processes = RunTestsAgentBridge(
        actions,
        Principal(tenant_id="example", subject_id="agent", framework="harnessix-agent"),
        repository,
        (
            TestProfile(
                name="unit",
                description="固定离线单元测试",
                program="python",
                arguments=("-I", "verify.py"),
                timeout_seconds=5,
            ),
        ),
    )
    sessions = SQLiteSessionStore(root / "session.db")
    artifacts = SQLiteArtifactStore(sessions)
    try:
        with copy:
            async with (
                ManagedPatchBridge(copy) as patches,
                CodingToolRuntime(
                    repository,
                    artifacts=artifacts,
                    git_executable=git_executable(),
                ) as tools,
            ):
                publisher = SQLiteProcessArtifactPublisher(
                    artifacts, processes, workspace_scope=tools.workspace_scope
                )
                async with AgentRuntime(
                    sessions,
                    FeedbackProvider(),
                    scoped_tools=tools,
                    artifacts=artifacts,
                    patches=patches,
                    processes=processes,
                    process_artifacts=publisher,
                ) as runtime:
                    thread = await runtime.create_thread(str(tools.workspace_root))
                    turn = await runtime.run_turn(
                        thread.thread_id, "修复加法并验证", request_id="coding-feedback"
                    )
                    assert turn.status is TurnStatus.WAITING_APPROVAL
                    await approve_process(runtime, thread.thread_id, turn)
                    assert await ActionWorker(actions, poll_seconds=0.01).run_once() is not None

                    turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                    patch = next(
                        item.content
                        for item in turn.items
                        if isinstance(item.content, PatchApprovalRequestContent)
                        and item.content.decision is None
                    )
                    await runtime.reply_approval(
                        thread.thread_id,
                        turn.turn_id,
                        patch.approval_id,
                        fingerprint=patch.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="example-reviewer"
                        ),
                    )
                    turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                    await approve_process(runtime, thread.thread_id, turn)
                    assert await ActionWorker(actions, poll_seconds=0.01).run_once() is not None
                    completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                    assert completed.status is TurnStatus.COMPLETED
                    assert (
                        repository.joinpath("calc.py")
                        .read_text(encoding="utf-8")
                        .endswith("return a + b\n")
                    )
        assert source.joinpath("calc.py").read_text(encoding="utf-8").endswith("return a - b\n")
    finally:
        await actions.close()
    print("失败测试→日志→审批Patch→测试通过→Git差异→回答闭环通过；源目录未修改。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-coding-feedback-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
