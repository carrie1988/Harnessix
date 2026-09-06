from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

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
from harnessix.processes.test_profiles import RunTestsAgentBridge
from harnessix.processes.test_profiles import TestProfile as Profile
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace
from harnessix.worker import ActionWorker


def _git() -> Path:
    executable = shutil.which("git")
    assert executable is not None
    return Path(executable).resolve()


def _git_command(root: Path, *arguments: str) -> None:
    subprocess.run(
        [str(_git()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def _result(request: ModelRequest, tool: str, index: int = -1) -> ToolResultContent:
    calls = [
        item.content for item in request.history if isinstance(item.content, ToolResultContent)
    ]
    matching = [item for item in calls if _tool_for_result(request, item) == tool]
    return matching[index]


def _tool_for_result(request: ModelRequest, result: ToolResultContent) -> str:
    return next(
        item.content.tool
        for item in request.history
        if getattr(item.content, "call_id", None) == result.call_id
        and getattr(item.content, "kind", None) == "tool_call"
    )


class FeedbackProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request.model_copy(deep=True))
        cancel.checkpoint()
        tool: str | None
        arguments: dict[str, object] | None
        if request.step == 1:
            tool, arguments = "run_tests", {"profile": "unit"}
        elif request.step == 2:
            failed = _result(request, "run_tests")
            assert failed.output["passed"] is False and failed.output["returncode"] == 1
            tool, arguments = (
                "read_artifact",
                {
                    "artifact_id": failed.output["artifact"]["artifact_id"],
                    "offset": 0,
                    "limit": 100,
                },
            )
        elif request.step == 3:
            page = _result(request, "read_artifact").output
            document = parse_process_output_document(page["text"].encode())
            assert b"expected 5" in b"".join(
                chunk.data() for chunk in document.chunks if chunk.stream == "stderr"
            )
            tool, arguments = "read_file", {"path": "calc.py"}
        elif request.step == 4:
            source = _result(request, "read_file").output
            assert "return a - b" in source["text"]
            tool, arguments = (
                "apply_patch",
                {
                    "path": "calc.py",
                    "expected_revision": source["revision"],
                    "edits": [{"old_text": "return a - b", "new_text": "return a + b"}],
                },
            )
        elif request.step == 5:
            patch = _result(request, "apply_patch")
            assert patch.patch is not None and patch.patch.state == "applied"
            tool, arguments = "run_tests", {"profile": "unit"}
        elif request.step == 6:
            passed = _result(request, "run_tests")
            assert passed.output["passed"] is True and passed.output["returncode"] == 0
            tool, arguments = "git_status", {}
        elif request.step == 7:
            status = _result(request, "git_status").output
            assert status["total_entries"] == 1
            assert status["entries"][0]["path"] == "calc.py"
            tool, arguments = "git_diff", {"target": "worktree", "context_lines": 1}
        else:
            assert request.step == 8
            diff = _result(request, "git_diff").output
            assert "-    return a - b" in diff["text"]
            assert "+    return a + b" in diff["text"]
            yield ResponseStarted(response_id="feedback-8")
            yield TextStarted(content_id="answer")
            yield TextCompleted(
                content_id="answer", text="已修复加法逻辑；unit 测试通过，Git 差异已核对。"
            )
            yield ResponseCompleted()
            return
        yield ResponseStarted(response_id=f"feedback-{request.step}")
        assert tool is not None and arguments is not None
        yield ToolCallCompleted(
            call_id=f"feedback-call-{request.step}", tool=tool, arguments=arguments
        )
        yield ResponseCompleted(finish_reason="tool_calls")


def _process_approval(turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in reversed(turn.items)
        if isinstance(item.content, ProcessApprovalRequestContent) and item.content.decision is None
    )


async def test_failed_test_patch_passed_test_and_git_feedback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (source / "verify.py").write_text(
        "import runpy\nns = runpy.run_path('calc.py')\n"
        "assert ns['add'](2, 3) == 5, 'expected 5'\nprint('PASS')\n",
        encoding="utf-8",
    )
    factory = PatchWorkspaces(tmp_path / "private")
    with Workspace(source) as source_workspace:
        copy = factory.create(source_workspace, ("calc.py", "verify.py"), ReadOperation())
    root = copy.workspace.root
    _git_command(root, "init", "-q")
    _git_command(root, "config", "user.name", "Harnessix Test")
    _git_command(root, "config", "user.email", "test@harnessix.invalid")
    _git_command(root, "add", "calc.py", "verify.py")
    _git_command(root, "commit", "-qm", "baseline")

    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    processes = RunTestsAgentBridge(
        actions,
        Principal(tenant_id="t", subject_id="agent", framework="harnessix-agent"),
        root,
        (
            Profile(
                name="unit",
                description="固定加法验收",
                program="python",
                arguments=("-I", "verify.py"),
                timeout_seconds=5,
            ),
        ),
    )
    sessions = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(sessions)
    provider = FeedbackProvider()
    try:
        with copy:
            async with (
                ManagedPatchBridge(copy) as patches,
                CodingToolRuntime(root, artifacts=artifacts, git_executable=_git()) as tools,
            ):
                publisher = SQLiteProcessArtifactPublisher(
                    artifacts, processes, workspace_scope=tools.workspace_scope
                )
                async with AgentRuntime(
                    sessions,
                    provider,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    patches=patches,
                    processes=processes,
                    process_artifacts=publisher,
                ) as runtime:
                    thread = await runtime.create_thread(str(tools.workspace_root))
                    first = await runtime.run_turn(
                        thread.thread_id, "修复加法并验证", request_id="feedback-loop"
                    )
                    assert first.status is TurnStatus.WAITING_APPROVAL
                    first_process = _process_approval(first)
                    await runtime.reply_approval(
                        thread.thread_id,
                        first.turn_id,
                        first_process.approval_id,
                        fingerprint=first_process.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="reviewer"
                        ),
                    )
                    assert await ActionWorker(actions, poll_seconds=0.01).run_once() is not None
                    patch_wait = await runtime.resume_turn(thread.thread_id, first.turn_id)
                    assert patch_wait.status is TurnStatus.WAITING_APPROVAL
                    patch_approval = next(
                        item.content
                        for item in patch_wait.items
                        if isinstance(item.content, PatchApprovalRequestContent)
                        and item.content.decision is None
                    )
                    await runtime.reply_approval(
                        thread.thread_id,
                        first.turn_id,
                        patch_approval.approval_id,
                        fingerprint=patch_approval.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="reviewer"
                        ),
                    )
                    second = await runtime.resume_turn(thread.thread_id, first.turn_id)
                    assert second.status is TurnStatus.WAITING_APPROVAL, (
                        second.error,
                        len(provider.requests),
                    )
                    second_process = _process_approval(second)
                    await runtime.reply_approval(
                        thread.thread_id,
                        first.turn_id,
                        second_process.approval_id,
                        fingerprint=second_process.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="reviewer"
                        ),
                    )
                    assert await ActionWorker(actions, poll_seconds=0.01).run_once() is not None
                    completed = await runtime.resume_turn(thread.thread_id, first.turn_id)
                assert completed.status is TurnStatus.COMPLETED
                assert len(provider.requests) == 8
                assert (root / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
        assert (source / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    finally:
        await actions.close()
