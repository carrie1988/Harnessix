from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import httpx2
import pytest

from harnessix.agent.models import ProcessApprovalRequestContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.test_profiles import RunTestsAgentBridge
from harnessix.processes.test_profiles import TestProfile as Profile
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker
from tests.models import anthropic_wire as aw
from tests.models import wire as ow


def _git() -> Path:
    executable = shutil.which("git")
    assert executable is not None
    return Path(executable).resolve()


def _command(root: Path, *arguments: str) -> None:
    subprocess.run(
        [str(_git()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def _outputs(body: dict[str, object], vendor: str) -> list[dict[str, object]]:
    messages = body["messages"]
    assert isinstance(messages, list)
    if vendor == "openai":
        return [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
    return [
        json.loads(content["content"])
        for message in messages
        if isinstance(message["content"], list)
        for content in message["content"]
        if content["type"] == "tool_result"
    ]


def _tool_frames(vendor: str, index: int, name: str, arguments: dict[str, object]):
    if vendor == "openai":
        return [
            ow.frame(
                ow.chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"feedback-sdk-{index}",
                                "type": "function",
                                "function": {
                                    "name": tool_alias(name),
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                )
            ),
            ow.frame(ow.chunk(finish="tool_calls")),
            ow.frame(ow.chunk(usage=True)),
            b"data: [DONE]\n\n",
        ]
    return [
        aw.start(),
        aw.frame(
            "content_block_start",
            index=0,
            content_block={
                "type": "tool_use",
                "id": f"feedback-sdk-{index}",
                "name": tool_alias(name),
                "input": {},
            },
        ),
        aw.frame(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": json.dumps(arguments)},
        ),
        aw.frame("content_block_stop", index=0),
        *aw.stop("tool_use"),
    ]


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
async def test_real_sdk_run_tests_git_status_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
    _command(root, "init", "-q")
    _command(root, "config", "user.name", "Harnessix Test")
    _command(root, "config", "user.email", "test@harnessix.invalid")
    _command(root, "add", "calc.py")
    _command(root, "commit", "-qm", "baseline")
    (root / "calc.py").write_text("value = 2\n", encoding="utf-8")
    marker = tmp_path / "test-count"
    code = (
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); print('PASS')"
    )
    monkeypatch.setenv("HARNESSIX_FEEDBACK_SDK_KEY", "offline-fixture")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    requests: list[dict[str, object]] = []
    streams: list[ow.WireStream | aw.WireStream] = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        previous = _outputs(body, vendor)
        tools = body["tools"]
        names = {item["function"]["name"] if vendor == "openai" else item["name"] for item in tools}
        assert {
            tool_alias("run_tests"),
            tool_alias("git_status"),
            tool_alias("git_diff"),
        } <= names
        if index == 1:
            name, arguments = "run_tests", {"profile": "unit"}
        elif index == 2:
            result = previous[-1]
            assert result["outcome"] == "succeeded"
            assert result["output"]["profile"] == "unit"
            assert result["output"]["passed"] is True
            name, arguments = "git_status", {}
        elif index == 3:
            result = previous[-1]
            assert result["output"]["entries"][0]["path"] == "calc.py"
            name, arguments = "git_diff", {"target": "worktree", "context_lines": 0}
        else:
            assert index == 4
            text = previous[-1]["output"]["text"]
            assert "-value = 1" in text and "+value = 2" in text
            name = None
            arguments = None
        wire = ow if vendor == "openai" else aw
        frames = (
            wire.text_frames() if name is None else _tool_frames(vendor, index, name, arguments)
        )
        stream = wire.WireStream(frames)
        streams.append(stream)
        return wire.response(stream)

    def provider():
        if vendor == "openai":
            return OpenAIChatProvider(
                OpenAIChatConfig(
                    model="test-model",
                    base_url="https://provider.invalid/v1",
                    api_key_env="HARNESSIX_FEEDBACK_SDK_KEY",
                    max_attempts=1,
                ),
                transport=httpx.MockTransport(handle),
            )
        return AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                base_url="https://provider.invalid",
                api_key_env="HARNESSIX_FEEDBACK_SDK_KEY",
                max_attempts=1,
            ),
            transport=httpx2.MockTransport(handle),
        )

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
    bridge = RunTestsAgentBridge(
        actions,
        Principal(tenant_id="t", subject_id="agent", framework="harnessix-agent"),
        root,
        (
            Profile(
                name="unit",
                description="固定SDK验收",
                program="python",
                arguments=("-I", "-c", code, str(marker)),
                timeout_seconds=5,
            ),
        ),
    )
    session = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(session)
    try:
        async with provider() as model:
            async with CodingToolRuntime(root, artifacts=artifacts, git_executable=_git()) as tools:
                publisher = SQLiteProcessArtifactPublisher(
                    artifacts, bridge, workspace_scope=tools.workspace_scope
                )
                async with AgentRuntime(
                    session,
                    model,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    processes=bridge,
                    process_artifacts=publisher,
                ) as runtime:
                    thread = await runtime.create_thread(str(tools.workspace_root))
                    pending = await runtime.run_turn(
                        thread.thread_id, "运行测试并核对Git差异", request_id="sdk-feedback"
                    )
                    assert pending.status is TurnStatus.WAITING_APPROVAL
                    approval = next(
                        item.content
                        for item in pending.items
                        if isinstance(item.content, ProcessApprovalRequestContent)
                    )
                    await runtime.reply_approval(
                        thread.thread_id,
                        pending.turn_id,
                        approval.approval_id,
                        fingerprint=approval.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="sdk-reviewer"
                        ),
                    )
                    assert await ActionWorker(actions, poll_seconds=0.01).run_once() is not None
                    completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
        assert completed.status is TurnStatus.COMPLETED
    finally:
        await actions.close()

    assert marker.read_text() == "1"
    assert len(requests) == 4 and all(stream.closed for stream in streams)
    public = json.dumps(requests)
    for private in (
        str(marker),
        code,
        str(approval.plan.action_id),
        approval.plan.action_fingerprint,
        approval.plan.approval_fingerprint,
        approval.plan.idempotency_key,
        "data_base64",
    ):
        assert private not in public
