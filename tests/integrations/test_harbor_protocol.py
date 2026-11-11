"""以真实Runtime、Session和公共SDK验证Harbor侧协议驱动，不调用付费模型。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import ProtocolLimits, PublicBudget
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harbor import protocol_runner
from integrations.harbor.harnessix_configs import COMMAND_TIMEOUT_SECONDS
from integrations.harbor.protocol_runner import drive_protocol_trial
from tests.agent.helpers import RecordingTools, answer, tool_step


def budget(timeout: float = 5.0) -> PublicBudget:
    return PublicBudget(
        max_steps=4,
        max_tokens=1000,
        timeout_seconds=timeout,
        max_output_chars=4096,
        max_tool_calls_per_step=4,
    )


@pytest.mark.parametrize("interactive", [False, True])
async def test_real_runtime_receipt_and_intervention_cancellation(
    tmp_path: Path, interactive: bool
) -> None:
    scripts = (
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="question", tool="ask_user", arguments={"question": "PRIVATE-QUESTION"}
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ]
        ]
        if interactive
        else [answer("PRIVATE-REPLY")]
    )
    provider = ScriptedProvider(scripts)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        async with client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="PRIVATE-INSTRUCTION",
                budget=budget(),
                trial_id=uuid4(),
            )
            assert receipt["reason"] == ("intervention_required" if interactive else "completed")
            assert receipt["cost_completeness"] == "unknown"
            assert receipt["turn_terminal_confirmed"]
            assert "PRIVATE-" not in json.dumps(receipt)
            assert len(provider.requests) == 1


@pytest.mark.parametrize("approve", [True, False])
async def test_approval_policy_answers_only_what_it_allows(tmp_path: Path, approve: bool) -> None:
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer("done")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        async with client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="read",
                budget=budget().model_copy(update={"max_tokens": 50_000}),
                trial_id=uuid4(),
                approval_mode="auto",
                # auto只批准补丁与沙箱内进程；测试工具既不是补丁也不在沙箱前缀内。
                policy=(lambda content, tool: "approved" if tool == "test.read" else None)
                if approve
                else None,
            )
    assert receipt["reason"] == ("completed" if approve else "intervention_required")
    assert receipt["auto_approvals"] == (1 if approve else 0)
    assert receipt["approval_mode"] == "auto"
    assert len(tools.calls) == (1 if approve else 0)


async def test_model_relay_forwards_loopback_port_to_target() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"pong:" + await reader.readline())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    target = upstream.sockets[0].getsockname()[1]
    (relay,) = await protocol_runner._start_relays([f"127.0.0.1:0=127.0.0.1:{target}"])
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping\n")
        await writer.drain()
        assert await reader.read() == b"pong:ping\n"
        writer.close()
    finally:
        relay.close()
        upstream.close()
    with pytest.raises(ValueError):
        await protocol_runner._start_relays([f"0.0.0.0:0=127.0.0.1:{target}"])


async def test_trial_deadline_covers_initialize_without_restarting_agent() -> None:
    class StalledClient:
        calls = 0

        async def initialize(self):
            self.calls += 1
            await asyncio.Event().wait()

    client = StalledClient()
    receipt = await drive_protocol_trial(
        client, workspace="/app", instruction="task", budget=budget(0.01), trial_id=uuid4()
    )
    assert receipt["reason"] == "deadline_exceeded"
    assert receipt["input_tokens"] is None
    assert client.calls == 1


async def test_server_that_never_initializes_is_unavailable_not_agent_failure() -> None:
    class ClosedServer:
        async def initialize(self):
            raise AgentSDKError("server_closed", "App Server输出流已经关闭")

    receipt = await drive_protocol_trial(
        ClosedServer(), workspace="/app", instruction="task", budget=budget(5), trial_id=uuid4()
    )
    assert receipt["reason"] == "server_unavailable"
    assert receipt["turn_terminal_confirmed"] is False


HISTORY_TIMEOUT = "publication_history_timeout"


class FlakyReads:
    """前failures次执行中的读取报code错误，其余调用原样转给真实客户端。"""

    def __init__(self, client: AgentClient, failures: int, code: str) -> None:
        self._client = client
        self.failures = failures
        self.code = code

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    async def _read(self, name: str, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise AgentSDKError(self.code, "注入的读取失败")
        return await getattr(self._client, name)(*args, **kwargs)

    async def get_thread(self, *args, **kwargs):
        return await self._read("get_thread", *args, **kwargs)

    async def next_events(self, *args, **kwargs):
        return await self._read("next_events", *args, **kwargs)


async def flaky_trial(
    tmp_path: Path, failures: int, code: str, read_retry: protocol_runner.ReadRetry
) -> dict:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, ScriptedProvider([answer("done")])) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        async with client:
            return await drive_protocol_trial(
                FlakyReads(client, failures, code),
                workspace=str(tmp_path),
                instruction="task",
                budget=budget(),
                trial_id=uuid4(),
                read_retry=read_retry,
            )


async def test_history_timeouts_while_reading_are_retried_and_counted(tmp_path: Path) -> None:
    retry = protocol_runner.ReadRetry(window_seconds=5, first_delay_seconds=0.001)
    receipt = await flaky_trial(tmp_path, 3, HISTORY_TIMEOUT, retry)
    assert receipt["reason"] == "completed"
    assert receipt["runner_error"] is None
    assert receipt["transient_read_errors"] == 3


async def test_history_timeouts_beyond_the_window_end_with_their_code(tmp_path: Path) -> None:
    retry = protocol_runner.ReadRetry(
        window_seconds=0.05, first_delay_seconds=0.001, max_delay_seconds=0.01
    )
    receipt = await flaky_trial(tmp_path, 10**6, HISTORY_TIMEOUT, retry)
    assert receipt["reason"] == "protocol_error"
    assert receipt["runner_error"] == f"AgentSDKError:{HISTORY_TIMEOUT}"
    assert receipt["transient_read_errors"] >= 1
    assert receipt["turn_terminal_confirmed"] is False


async def test_other_protocol_errors_are_not_retried(tmp_path: Path) -> None:
    retry = protocol_runner.ReadRetry(window_seconds=5, first_delay_seconds=0.001)
    receipt = await flaky_trial(tmp_path, 1, "replay_stalled", retry)
    assert receipt["reason"] == "protocol_error"
    assert receipt["runner_error"] == "AgentSDKError:replay_stalled"
    assert receipt["transient_read_errors"] == 0


async def test_read_retry_backs_off_exponentially_up_to_the_cap(monkeypatch) -> None:
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(protocol_runner.asyncio, "sleep", recording_sleep)
    failures = iter(range(6))

    async def read() -> str:
        if next(failures, None) is not None:
            raise AgentSDKError(HISTORY_TIMEOUT, "Session历史认证超时")
        return "page"

    retry = protocol_runner.ReadRetry(
        window_seconds=1000, first_delay_seconds=1, max_delay_seconds=8
    )
    assert await retry(read) == "page"
    assert delays == [1, 2, 4, 8, 8, 8]
    assert retry.absorbed == 6


async def test_oversized_event_pages_are_reread_in_smaller_pages(tmp_path: Path) -> None:
    # 事件页超过协商的消息上限时服务端拒绝整页；运行中读取和终态重放都不能因此丢掉试次或证据。
    reply = "R" * 1500
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, ScriptedProvider([answer(reply)])) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        server = AgentProtocolServer(service, limits=ProtocolLimits(max_message_bytes=4096))
        events: list = []
        async with AgentClient(InProcessAgentTransport(server)) as client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="task",
                budget=budget(),
                trial_id=uuid4(),
                events=events,
            )
    assert receipt["reason"] == "completed"
    assert receipt["protocol_evidence_complete"] is True
    assert receipt["final_assistant_sha256"] == hashlib.sha256(reply.encode()).hexdigest()
    assert events


async def test_event_pages_shrink_only_for_oversized_pages_and_stay_small() -> None:
    requested: list[int] = []

    async def fetch(*, limit: int) -> str:
        requested.append(limit)
        if limit > 16:
            code = "response_too_large" if limit > 32 else "public_output_limit"
            raise AgentSDKError(code, "事件页超限")
        return "page"

    pages = protocol_runner.EventPages(64)
    assert await pages.read(fetch) == "page"
    assert await pages.read(fetch) == "page"
    assert requested == [64, 32, 16, 16]

    def failing(code: str):
        async def fetch(*, limit: int) -> str:
            requested.append(limit)
            raise AgentSDKError(code, "读取失败")

        return fetch

    # 其他错误不重读；页长已是1仍超限说明单个事件放不下，原样报错。
    for code, start in (("replay_stalled", 64), ("public_output_limit", 1)):
        requested.clear()
        with pytest.raises(AgentSDKError):
            await protocol_runner.EventPages(start).read(failing(code))
        assert requested == [start]


def test_server_command_binds_git_only_on_request_and_when_detected(
    tmp_path: Path, monkeypatch
) -> None:
    arguments = argparse.Namespace(
        server_config="/etc/harnessix-eval/product.json",
        action_config=None,
        workspace=str(tmp_path),
        state_directory="/tmp/harnessix-state/trial",
        git_read_tools=False,
    )
    probed = []

    def detect(explicit: str | None, workspace: Path) -> str | None:
        probed.append((explicit, workspace))
        return "/usr/bin/git"

    monkeypatch.setattr(protocol_runner, "detected_git_executable", detect)
    plain = protocol_runner.server_command(arguments)
    assert plain[1:4] == ["-m", "harnessix", "agent-server"]
    assert "--git-executable" not in plain and probed == []

    arguments.git_read_tools = True
    bound = protocol_runner.server_command(arguments)
    assert bound == [*plain, "--git-executable", "/usr/bin/git"]
    # 与harnessix code、harnessix exec相同的探测：不显式指定路径，按工作区判断。
    assert probed == [(None, tmp_path.absolute())]

    # 工作区不在Git仓库内或git不可用：按未配置启动，试次照常进行。
    monkeypatch.setattr(protocol_runner, "detected_git_executable", lambda *_: None)
    assert protocol_runner.server_command(arguments) == plain


def test_default_read_window_outlasts_one_command() -> None:
    # CPU争抢来自任务命令本身；窗口短于命令期限会在长测试期间误杀试次。
    assert protocol_runner.ReadRetry().window_seconds > COMMAND_TIMEOUT_SECONDS


def test_startup_failure_still_writes_a_complete_receipt(tmp_path: Path, monkeypatch) -> None:
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("task")
    output = tmp_path / "result.json"
    trial = uuid4()
    monkeypatch.setattr(
        protocol_runner,
        "_run",
        AsyncMock(side_effect=AgentSDKError("server_closed", "App Server输出流已经关闭")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--instruction-file",
            str(instruction),
            "--server-config",
            str(tmp_path / "config.json"),
            "--workspace",
            "/app",
            "--state-directory",
            str(tmp_path / "state"),
            "--output",
            str(output),
            "--trial-id",
            str(trial),
            "--approval-mode",
            "auto",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        protocol_runner.main()
    assert exited.value.code == 1
    receipt = json.loads(output.read_text())
    assert receipt["spec_version"] == "harnessix.harbor-protocol-result/v1"
    assert (receipt["trial_id"], receipt["reason"]) == (str(trial), "server_unavailable")
    assert receipt["runner_error"] == "AgentSDKError:server_closed"
    assert receipt["approval_mode"] == "auto" and receipt["protocol_evidence_complete"] is False


def test_receipt_reservation_prevents_reexecution(tmp_path: Path, monkeypatch) -> None:
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("task")
    output = tmp_path / "result.json"
    state = tmp_path / "state"
    trial = uuid4()
    run = AsyncMock(return_value={"reason": "completed", "trial_id": str(trial)})
    monkeypatch.setattr(protocol_runner, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--instruction-file",
            str(instruction),
            "--server-config",
            str(tmp_path / "config.json"),
            "--workspace",
            "/app",
            "--state-directory",
            str(state),
            "--output",
            str(output),
            "--trial-id",
            str(trial),
        ],
    )
    with pytest.raises(SystemExit) as first:
        protocol_runner.main()
    assert first.value.code == 0
    before = output.read_bytes()
    with pytest.raises(SystemExit) as duplicate:
        protocol_runner.main()
    assert duplicate.value.code == 1
    assert run.await_count == 1
    assert output.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600


def test_existing_state_is_not_implicitly_restarted(tmp_path: Path, monkeypatch) -> None:
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("task")
    state = tmp_path / "state"
    state.mkdir()
    (state / "existing").write_text("preserve")
    run = AsyncMock()
    monkeypatch.setattr(protocol_runner, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--instruction-file",
            str(instruction),
            "--server-config",
            "/config.json",
            "--workspace",
            "/app",
            "--state-directory",
            str(state),
            "--output",
            str(tmp_path / "result.json"),
            "--trial-id",
            str(uuid4()),
        ],
    )
    with pytest.raises(SystemExit) as failure:
        protocol_runner.main()
    assert failure.value.code == 1
    assert run.await_count == 0
    assert (state / "existing").read_text() == "preserve"
