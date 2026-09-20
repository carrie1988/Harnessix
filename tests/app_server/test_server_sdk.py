from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
from collections.abc import Buffer
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from harnessix.agent.approvals import approval_for
from harnessix.agent.models import ItemDelta, QuestionRequestContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.server import AgentProtocolServer, ConnectionState
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    EventsNextParams,
    EventsNextResult,
    EventsReplayResult,
    InitializeResult,
    ProtocolLimits,
    PublicApprovalDecision,
    QuestionRespondParams,
    ServerCapabilities,
    ServerInfo,
    ThreadCreateParams,
    TurnResult,
    TurnStartParams,
)
from harnessix.protocol.projection import project_turn
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import (
    AgentClient,
    AgentSDKError,
    InProcessAgentTransport,
    SubprocessAgentTransport,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.artifacts.helpers import exercise, results
from tests.helpers import wait_for_turn_status


def _request(method: str, params: dict[str, object], *, request_id: int = 1) -> bytes:
    return (
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        ).encode()
        + b"\n"
    )


def _decoded(response: tuple[bytes, ...]) -> dict[str, object]:
    assert len(response) == 1
    value = json.loads(response[0])
    assert isinstance(value, dict)
    return value


async def _service(runtime: AgentRuntime, store: SQLiteSessionStore) -> AgentApplicationService:
    return AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))


async def test_handshake_enforces_state_version_and_params(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        server = AgentProtocolServer(await _service(runtime, store))
        before = _decoded(await server.process_frame(_request("thread/list", {})))
        assert before["error"]["data"]["code"] == "not_initialized"  # type: ignore[index]

        invalid = _decoded(
            await server.process_frame(
                _request(
                    "initialize",
                    {
                        "protocolVersion": "1.0",
                        "clientInfo": {"name": "test", "version": "1"},
                        "clientInstanceId": str(uuid4()),
                        "unknown": True,
                    },
                )
            )
        )
        assert invalid["error"]["data"]["code"] == "invalid_params"  # type: ignore[index]
        assert server.state is ConnectionState.NEW

        initialized = _decoded(
            await server.process_frame(
                _request(
                    "initialize",
                    {
                        "protocolVersion": "1.0",
                        "clientInfo": {"name": "test", "version": "1"},
                        "clientInstanceId": str(uuid4()),
                    },
                )
            )
        )
        assert initialized["result"]["protocolVersion"] == "1.0"  # type: ignore[index]
        assert not initialized["result"]["capabilities"]["itemDeltas"]  # type: ignore[index]
        assert server.state.value == ConnectionState.INITIALIZED_PENDING_ACK.value
        assert (
            await server.process_frame(
                b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
            )
            == ()
        )
        assert server.state.value == ConnectionState.READY.value


async def test_sdk_serializes_concurrent_initialize_calls(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        server = AgentProtocolServer(await _service(runtime, store))
        client = AgentClient(InProcessAgentTransport(server))
        first, second = await asyncio.gather(client.initialize(), client.initialize())
        assert first is second
        await client.close()


class _ReplyTransport:
    def __init__(self, result: object, *, notify_error: AgentSDKError | None = None) -> None:
        self.result = result
        self.notify_error = notify_error
        self.exchange_calls = 0
        self.notify_calls = 0
        self.close_calls = 0

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        self.exchange_calls += 1
        request = json.loads(frame)
        return (
            json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": self.result},
                separators=(",", ":"),
            ).encode()
            + b"\n",
        )

    async def notify(self, frame: bytes) -> None:
        del frame
        self.notify_calls += 1
        if self.notify_error is not None:
            raise self.notify_error

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(
    "response",
    (
        b'{"jsonrpc":"2.0","id":true,"result":{}}\n',
        b'{"jsonrpc":"1.0","id":1,"result":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"result":{},"future":true}\n',
        b'{"jsonrpc":"2.0","id":1,"result":{},"error":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"result":NaN}\n',
    ),
)
async def test_sdk_rejects_invalid_response_envelopes(response: bytes) -> None:
    class InvalidEnvelopeTransport:
        async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
            del frame
            return (response,)

        async def notify(self, frame: bytes) -> None:
            del frame

        async def close(self) -> None:
            return None

    client = AgentClient(InvalidEnvelopeTransport())
    with pytest.raises(AgentSDKError) as error:
        await client._send("thread/list", {})
    assert error.value.code == "invalid_response"


async def test_sdk_rejects_response_over_json_depth_budget() -> None:
    nested: object = None
    for _ in range(65):
        nested = {"nested": nested}
    response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": nested}).encode() + b"\n"

    class DeepResponseTransport:
        async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
            del frame
            return (response,)

        async def notify(self, frame: bytes) -> None:
            del frame

        async def close(self) -> None:
            return None

    client = AgentClient(DeepResponseTransport())
    with pytest.raises(AgentSDKError) as error:
        await client._send("thread/list", {})
    assert error.value.code == "invalid_response"


async def test_sdk_normalizes_invalid_result_contract() -> None:
    transport = _ReplyTransport({"threads": [], "nextCursor": 7})
    client = AgentClient(transport)

    with pytest.raises(AgentSDKError) as error:
        await client.list_threads()

    assert error.value.code == "invalid_result"
    assert error.value.path == ("nextCursor",)


async def test_initialize_notification_failure_makes_connection_unusable() -> None:
    result = InitializeResult(
        server_info=ServerInfo(version="0.9.1-test"),
        capabilities=ServerCapabilities(methods=("thread/list",)),
        limits=ProtocolLimits(),
    ).model_dump(mode="json", by_alias=True)
    transport = _ReplyTransport(
        result,
        notify_error=AgentSDKError("server_closed", "初始化通知发送失败"),
    )
    client = AgentClient(transport)

    with pytest.raises(AgentSDKError) as first:
        await client.initialize()
    assert first.value.code == "server_closed"
    assert transport.close_calls == 1

    with pytest.raises(AgentSDKError) as second:
        await client.initialize()
    assert second.value.code == "handshake_failed"
    assert transport.exchange_calls == 1


async def test_sdk_rejects_unadvertised_method_before_transport_write() -> None:
    result = InitializeResult(
        server_info=ServerInfo(version="0.9.1-test"),
        capabilities=ServerCapabilities(methods=("thread/get",)),
        limits=ProtocolLimits(),
    ).model_dump(mode="json", by_alias=True)
    transport = _ReplyTransport(result)
    client = AgentClient(transport)
    await client.initialize()

    with pytest.raises(AgentSDKError) as error:
        await client.list_threads()

    assert error.value.code == "method_not_negotiated"
    assert transport.exchange_calls == 1


async def test_sdk_enforces_negotiated_replay_and_message_limits_before_write() -> None:
    result = InitializeResult(
        server_info=ServerInfo(version="0.9.1-test"),
        capabilities=ServerCapabilities(methods=("events/replay", "turn/start")),
        limits=ProtocolLimits(max_message_bytes=4096, max_replay_events=4),
    ).model_dump(mode="json", by_alias=True)
    transport = _ReplyTransport(result)
    client = AgentClient(transport)
    await client.initialize()

    with pytest.raises(AgentSDKError) as replay_error:
        await client.replay_events(uuid4(), limit=5)
    assert replay_error.value.code == "negotiated_limit_exceeded"

    with pytest.raises(AgentSDKError) as message_error:
        await client.start_turn(uuid4(), "x" * 5000, request_id="large")
    assert message_error.value.code == "negotiated_limit_exceeded"
    assert transport.exchange_calls == 1


async def test_subprocess_transport_rejects_oversized_response_frame() -> None:
    child = (
        "import json,sys; "
        "request=json.loads(sys.stdin.buffer.readline()); "
        "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'text':'x'*5000}}),"
        "flush=True)"
    )
    transport = SubprocessAgentTransport(
        (sys.executable, "-c", child),
        max_message_bytes=4096,
    )

    with pytest.raises(AgentSDKError) as error:
        await transport.exchange(b'{"jsonrpc":"2.0","id":1,"method":"x","params":{}}\n')

    assert error.value.code == "invalid_response"
    await transport.close()


async def test_internal_validation_failure_is_not_reported_as_invalid_params(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        service = await _service(runtime, store)
        server = AgentProtocolServer(service)
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()

        async def broken_projection(_params):
            ThreadCreateParams(request_id="", workspace=str(tmp_path))
            raise AssertionError("unreachable")

        monkeypatch.setattr(service, "list_threads", broken_projection)
        with pytest.raises(AgentSDKError) as error:
            await client.list_threads()
        assert error.value.code == "internal_error"
        await client.close()


async def test_agent_sdk_drives_turn_replay_and_duplicate_command(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = FakeProvider("协议闭环完成")
    client_id = uuid4()
    async with AgentRuntime(store, provider) as runtime:
        service = await _service(runtime, store)
        first = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)),
            client_instance_id=client_id,
        )
        await first.initialize()
        thread = await first.create_thread(str(tmp_path), request_id="create-1")
        accepted = await first.start_turn(thread.thread_id, "完成协议测试", request_id="turn-1")
        assert accepted.status == "accepted"
        await wait_for_turn_status(first, thread.thread_id, "completed")
        await service.close()

        second = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)),
            client_instance_id=client_id,
        )
        await second.initialize()
        duplicate = await second.start_turn(thread.thread_id, "完成协议测试", request_id="turn-1")
        snapshot = await second.get_thread(thread.thread_id)
        replay = await second.replay_events(thread.thread_id, limit=100)

        assert duplicate.turn_id == accepted.turn_id
        assert snapshot.latest_turn is not None
        assert snapshot.latest_turn.status == "completed"
        assert replay.scanned_through == snapshot.cursor
        assert replay.events[-1].cursor <= replay.scanned_through
        assert len(provider.requests) == 1
        await second.close()


async def test_same_command_key_with_different_prompt_returns_conflict(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(await _service(runtime, store)))
        )
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create")
        await client.start_turn(thread.thread_id, "输入一", request_id="same")
        try:
            await client.start_turn(thread.thread_id, "输入二", request_id="same")
        except Exception as error:
            assert getattr(error, "code", None) == "idempotency_conflict"
        else:
            raise AssertionError("参数漂移必须失败")
        await client.close()


async def test_completed_ledger_recovers_accepted_turn_after_restart(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    requests = SQLiteProtocolRequestStore(store.path)
    client_id = uuid4()
    params: TurnStartParams
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        params = TurnStartParams(
            request_id="crash-after-complete",
            thread_id=thread.thread_id,
            prompt="恢复已经接受的Turn",
        )
        await requests.claim(
            client_id,
            params.request_id,
            "turn/start",
            params.model_dump(mode="json", by_alias=True),
        )
        accepted = await runtime.accept_turn(
            thread.thread_id,
            params.prompt,
            request_id=params.request_id,
        )
        await requests.complete(
            client_id,
            params.request_id,
            TurnResult(turn=project_turn(accepted)).model_dump(mode="json", by_alias=True),
        )

    provider = FakeProvider("重启恢复完成")
    async with AgentRuntime(store, provider) as runtime:
        service = AgentApplicationService(runtime, store, requests)
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)),
            client_instance_id=client_id,
        )
        await client.initialize()
        duplicate = await client.start_turn(
            params.thread_id,
            params.prompt,
            request_id=params.request_id,
        )
        assert duplicate.status == "accepted"
        current = await wait_for_turn_status(client, params.thread_id, "completed")
        await service.close()
        assert current.latest_turn is not None
        assert current.latest_turn.status == "completed"
        assert len(provider.requests) == 1
        await client.close()


async def test_thread_list_filters_before_pagination(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(await _service(runtime, store)))
        )
        await client.initialize()
        first = await client.create_thread(str(tmp_path / "one"), request_id="one")
        second = await client.create_thread(str(tmp_path / "two"), request_id="two")
        await client.archive_thread(first.thread_id, request_id="archive")

        active = await client.list_threads(archived=False, limit=1)
        archived = await client.list_threads(archived=True, limit=1)

        assert [item.thread_id for item in active.threads] == [second.thread_id]
        assert active.next_cursor is None
        assert [item.thread_id for item in archived.threads] == [first.thread_id]
        assert archived.next_cursor is None
        await client.close()


async def test_service_shutdown_is_bounded_and_persists_cancel(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider(
        [[ResponseStarted(response_id="slow"), ResponseCompleted()]],
        delay_seconds=10,
    )
    async with AgentRuntime(store, provider) as runtime:
        service = await _service(runtime, store)
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create")
        turn = await client.start_turn(thread.thread_id, "慢请求", request_id="slow")
        await service.close(grace_seconds=0.01)
        current = await client.get_thread(thread.thread_id)
        assert current.latest_turn is not None
        assert current.latest_turn.turn_id == turn.turn_id
        assert current.latest_turn.status == "cancelled"
        await client.close()


async def test_subprocess_notification_does_not_wait_for_response() -> None:
    child = (
        "import json,sys; "
        "json.loads(sys.stdin.buffer.readline()); "
        "request=json.loads(sys.stdin.buffer.readline()); "
        "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{}}),flush=True)"
    )
    transport = SubprocessAgentTransport((sys.executable, "-c", child))
    await transport.notify(b'{"jsonrpc":"2.0","method":"ready","params":{}}\n')
    responses = await transport.exchange(b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}\n')
    assert json.loads(responses[0]) == {"jsonrpc": "2.0", "id": 7, "result": {}}
    await transport.close()


async def test_subprocess_transport_routes_out_of_order_responses() -> None:
    child = "\n".join(
        (
            "import json, sys",
            "first = json.loads(sys.stdin.buffer.readline())",
            "second = json.loads(sys.stdin.buffer.readline())",
            "for request in (second, first):",
            "    response = {'jsonrpc': '2.0', 'id': request['id'], "
            "'result': {'method': request['method']}}",
            "    print(json.dumps(response), flush=True)",
        )
    )
    transport = SubprocessAgentTransport((sys.executable, "-c", child))
    client = AgentClient(transport)
    first = asyncio.create_task(client._send("first", {}))
    await asyncio.sleep(0)
    second = asyncio.create_task(client._send("second", {}))

    assert await asyncio.gather(first, second) == [
        {"method": "first"},
        {"method": "second"},
    ]
    await client.close()


async def test_subprocess_transport_fails_all_pending_on_malformed_response() -> None:
    child = "\n".join(
        (
            "import sys",
            "sys.stdin.buffer.readline()",
            "sys.stdin.buffer.readline()",
            "print('not-json', flush=True)",
        )
    )
    transport = SubprocessAgentTransport((sys.executable, "-c", child))
    first = asyncio.create_task(
        transport.exchange(b'{"jsonrpc":"2.0","id":1,"method":"first","params":{}}\n')
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        transport.exchange(b'{"jsonrpc":"2.0","id":2,"method":"second","params":{}}\n')
    )
    errors = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(error, AgentSDKError) for error in errors)
    assert {error.code for error in errors if isinstance(error, AgentSDKError)} == {
        "invalid_response"
    }
    await transport.close()


async def test_subprocess_transport_bounds_cancelled_and_pending_requests() -> None:
    child = "\n".join(
        (
            "import json, sys, time",
            "for index in range(2):",
            "    request = json.loads(sys.stdin.buffer.readline())",
            "    if index == 0:",
            "        time.sleep(0.2)",
            "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'index': index}}",
            "    print(json.dumps(response), flush=True)",
        )
    )
    transport = SubprocessAgentTransport(
        (sys.executable, "-c", child),
        max_pending_requests=1,
    )
    first = asyncio.create_task(
        transport.exchange(b'{"jsonrpc":"2.0","id":1,"method":"first","params":{}}\n')
    )
    await asyncio.sleep(0.02)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(
        transport.exchange(b'{"jsonrpc":"2.0","id":2,"method":"second","params":{}}\n')
    )
    await asyncio.sleep(0.05)
    assert not second.done()
    snapshot = transport.snapshot()
    assert snapshot.abandoned_requests == 1
    assert snapshot.pending_requests == 0
    assert snapshot.max_pending_requests == 1

    response = await asyncio.wait_for(second, timeout=1)
    assert json.loads(response[0])["result"] == {"index": 1}
    assert transport.snapshot().abandoned_requests == 0
    await transport.close()


async def test_subprocess_transport_close_continues_after_caller_cancel() -> None:
    child = "import sys,time; sys.stdin.buffer.readline(); time.sleep(0.2)"
    transport = SubprocessAgentTransport(
        (sys.executable, "-c", child),
        graceful_shutdown_timeout_seconds=1,
        terminate_timeout_seconds=1,
    )
    await transport.notify(b'{"jsonrpc":"2.0","method":"ready","params":{}}\n')

    closing = asyncio.create_task(transport.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    await asyncio.wait_for(transport.close(), timeout=1)
    assert transport.snapshot().state == "closed"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"max_pending_requests": 0}, "App Server传输限制无效"),
        ({"graceful_shutdown_timeout_seconds": 0}, "App Server关闭超时必须大于零"),
        ({"terminate_timeout_seconds": 0}, "App Server关闭超时必须大于零"),
    ),
)
def test_subprocess_transport_rejects_invalid_resource_limits(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SubprocessAgentTransport((sys.executable, "-c", "pass"), **kwargs)  # type: ignore[arg-type]


def test_thread_create_rejects_relative_or_nul_workspace() -> None:
    with pytest.raises(ValueError):
        ThreadCreateParams(request_id="relative", workspace="relative")
    with pytest.raises(ValueError):
        ThreadCreateParams(request_id="nul", workspace="/tmp/a\x00b")


async def test_stdio_uses_jsonl_and_closes_on_eof(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    client_id = uuid4()
    incoming = io.BytesIO(
        _request(
            "initialize",
            {
                "protocolVersion": "1.0",
                "clientInfo": {"name": "stdio-test", "version": "1"},
                "clientInstanceId": str(client_id),
            },
        )
        + b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        + _request("thread/list", {}, request_id=2)
    )
    outgoing = io.BytesIO()
    async with AgentRuntime(store, FakeProvider()) as runtime:
        server = AgentProtocolServer(await _service(runtime, store))
        await run_stdio(server, incoming, outgoing)
        assert server.state is ConnectionState.CLOSED
    lines = outgoing.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"threads": [], "nextCursor": None},
    }


async def test_stdio_long_poll_does_not_block_concurrent_request(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    client_id = uuid4()
    read_fd, write_fd = os.pipe()
    incoming = os.fdopen(read_fd, "rb", buffering=0)
    producer = os.fdopen(write_fd, "wb", buffering=0)
    outgoing = io.BytesIO()
    try:
        async with AgentRuntime(store, FakeProvider()) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            cursor = (await store.events(thread.thread_id))[-1].sequence
            server = AgentProtocolServer(await _service(runtime, store))
            task = asyncio.create_task(run_stdio(server, incoming, outgoing))
            producer.write(
                _request(
                    "initialize",
                    {
                        "protocolVersion": "1.0",
                        "clientInfo": {"name": "multiplex-test", "version": "1"},
                        "clientInstanceId": str(client_id),
                    },
                )
                + b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
                + _request(
                    "events/next",
                    EventsNextParams(
                        thread_id=thread.thread_id,
                        after_cursor=cursor,
                        wait_ms=30_000,
                    ).model_dump(mode="json", by_alias=True),
                    request_id=2,
                )
                + _request("thread/list", {}, request_id=3)
            )
            producer.flush()

            responses: dict[int, dict[str, object]] = {}
            for _ in range(100):
                responses = {
                    value["id"]: value
                    for line in outgoing.getvalue().splitlines()
                    if isinstance((value := json.loads(line)).get("id"), int)
                }
                if 3 in responses:
                    break
                await asyncio.sleep(0.01)
            assert 3 in responses
            assert 2 not in responses

            producer.close()
            await asyncio.wait_for(task, timeout=2)
            assert server.state is ConnectionState.CLOSED
    finally:
        if not producer.closed:
            producer.close()
        incoming.close()


async def test_stdio_honors_negotiated_pending_request_limit(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    read_fd, write_fd = os.pipe()
    incoming = os.fdopen(read_fd, "rb", buffering=0)
    producer = os.fdopen(write_fd, "wb", buffering=0)
    outgoing = io.BytesIO()
    try:
        async with AgentRuntime(store, FakeProvider()) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            cursor = (await store.events(thread.thread_id))[-1].sequence
            server = AgentProtocolServer(await _service(runtime, store))
            task = asyncio.create_task(run_stdio(server, incoming, outgoing))
            producer.write(
                _request(
                    "initialize",
                    {
                        "protocolVersion": "1.0",
                        "clientInfo": {"name": "pending-limit-test", "version": "1"},
                        "clientInstanceId": str(uuid4()),
                        "limits": {"maxPendingRequests": 1},
                    },
                )
                + b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
                + _request(
                    "events/next",
                    EventsNextParams(
                        thread_id=thread.thread_id,
                        after_cursor=cursor,
                        wait_ms=150,
                    ).model_dump(mode="json", by_alias=True),
                    request_id=2,
                )
                + _request("thread/list", {}, request_id=3)
            )
            producer.flush()

            await asyncio.sleep(0.05)
            early_ids = {
                value["id"]
                for line in outgoing.getvalue().splitlines()
                if isinstance((value := json.loads(line)).get("id"), int)
            }
            assert 2 not in early_ids
            assert 3 not in early_ids

            for _ in range(100):
                response_ids = {
                    value["id"]
                    for line in outgoing.getvalue().splitlines()
                    if isinstance((value := json.loads(line)).get("id"), int)
                }
                if {2, 3} <= response_ids:
                    break
                await asyncio.sleep(0.01)
            assert {2, 3} <= response_ids

            producer.close()
            await asyncio.wait_for(task, timeout=2)
            assert server.state is ConnectionState.CLOSED
    finally:
        if not producer.closed:
            producer.close()
        incoming.close()


async def test_stdio_closes_slow_client_without_session_damage(tmp_path: Path) -> None:
    class BlockingOutput(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def write(self, value: Buffer) -> int:
            self.entered.set()
            self.release.wait(timeout=5)
            return super().write(value)

    store = SQLiteSessionStore(tmp_path / "session.db")
    client_id = uuid4()
    initialize = _request(
        "initialize",
        {
            "protocolVersion": "1.0",
            "clientInfo": {"name": "slow-test", "version": "1"},
            "clientInstanceId": str(client_id),
            "limits": {"maxOutboundMessages": 8},
        },
    )
    incoming = io.BytesIO(
        initialize
        + b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        + b"".join(_request("thread/list", {}, request_id=index) for index in range(2, 20))
    )
    outgoing = BlockingOutput()
    async with AgentRuntime(store, FakeProvider()) as runtime:
        server = AgentProtocolServer(await _service(runtime, store))
        task = asyncio.create_task(
            run_stdio(server, incoming, outgoing, outbound_timeout_seconds=0.01)
        )
        try:
            with pytest.raises(TimeoutError, match="stdio出站写入未在期限内完成"):
                await asyncio.wait_for(task, timeout=1)
        finally:
            outgoing.release.set()
        assert server.state is ConnectionState.CLOSED
        assert await store.thread_ids() == []


async def test_stdio_writer_failure_wakes_open_input_and_closes_server(tmp_path: Path) -> None:
    class FailingOutput(io.BytesIO):
        def write(self, value: Buffer) -> int:
            del value
            threading.Event().wait(timeout=0.05)
            raise OSError("simulated stdout failure")

    store = SQLiteSessionStore(tmp_path / "session.db")
    read_fd, write_fd = os.pipe()
    incoming = os.fdopen(read_fd, "rb", buffering=0)
    producer = os.fdopen(write_fd, "wb", buffering=0)
    outgoing = FailingOutput()
    try:
        async with AgentRuntime(store, FakeProvider()) as runtime:
            server = AgentProtocolServer(await _service(runtime, store))
            task = asyncio.create_task(run_stdio(server, incoming, outgoing))
            producer.write(
                _request(
                    "initialize",
                    {
                        "protocolVersion": "1.0",
                        "clientInfo": {"name": "writer-failure-test", "version": "1"},
                        "clientInstanceId": str(uuid4()),
                    },
                )
            )
            producer.flush()

            with pytest.raises(OSError, match="simulated stdout failure"):
                await asyncio.wait_for(task, timeout=1)
            assert server.state is ConnectionState.CLOSED
            assert not producer.closed
    finally:
        producer.close()
        incoming.close()


async def test_sdk_question_response_resumes_background_turn(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="ask",
                    tool="ask_user",
                    arguments={"question": "选择环境", "options": ["测试", "生产"]},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("继续完成"),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        service = await _service(runtime, store)
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create-question")
        accepted = await client.start_turn(thread.thread_id, "准备发布", request_id="turn-question")

        await wait_for_turn_status(client, thread.thread_id, "waiting_input")
        internal = await store.get_thread(thread.thread_id)
        request = next(
            item.content
            for item in internal.turns[-1].items
            if isinstance(item.content, QuestionRequestContent)
        )
        responded = await client.respond_question(
            QuestionRespondParams(
                request_id="answer-question",
                thread_id=thread.thread_id,
                turn_id=accepted.turn_id,
                question_id=request.question_id,
                answer="生产",
            )
        )
        assert responded.status == "executing_tools"
        duplicate = await client.respond_question(
            QuestionRespondParams(
                request_id="answer-question",
                thread_id=thread.thread_id,
                turn_id=accepted.turn_id,
                question_id=request.question_id,
                answer="生产",
            )
        )
        assert duplicate == responded
        completed = await wait_for_turn_status(client, thread.thread_id, "completed")
        await service.close()
        assert completed.latest_turn is not None
        assert completed.latest_turn.status == "completed"
        assert len(provider.requests) == 2
        await client.close()


async def test_completed_question_command_recovers_before_background_spawn(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    requests = SQLiteProtocolRequestStore(store.path)
    client_id = uuid4()
    params: QuestionRespondParams
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="ask",
                    tool="ask_user",
                    arguments={"question": "选择环境"},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("恢复完成"),
        ]
    )
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        waiting = await runtime.run_turn(thread.thread_id, "准备发布", request_id="turn")
        question = next(
            item.content
            for item in waiting.items
            if isinstance(item.content, QuestionRequestContent)
        )
        params = QuestionRespondParams(
            request_id="answer-before-spawn",
            thread_id=thread.thread_id,
            turn_id=waiting.turn_id,
            question_id=question.question_id,
            answer="生产",
        )
        await requests.claim(
            client_id,
            params.request_id,
            "question/respond",
            params.model_dump(mode="json", by_alias=True),
        )
        answered = await runtime.reply_question(
            thread.thread_id,
            waiting.turn_id,
            question.question_id,
            answer=params.answer,
        )
        await requests.complete(
            client_id,
            params.request_id,
            TurnResult(turn=project_turn(answered)).model_dump(mode="json", by_alias=True),
        )

    resumed_provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="ask",
                    tool="ask_user",
                    arguments={"question": "选择环境"},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("恢复完成"),
        ]
    )
    async with AgentRuntime(store, resumed_provider, enable_questions=True) as runtime:
        service = AgentApplicationService(runtime, store, requests)
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)),
            client_instance_id=client_id,
        )
        await client.initialize()
        duplicate = await client.respond_question(params)
        assert duplicate.status == "executing_tools"
        current = await wait_for_turn_status(client, params.thread_id, "completed")
        await service.close()
        assert current.latest_turn is not None and current.latest_turn.status == "completed"
        assert len(resumed_provider.requests) == 1
        await client.close()


async def test_events_next_delivers_live_delta_then_durable_replay(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([answer("流式文本")], delay_seconds=0.02)
    async with AgentRuntime(store, provider) as runtime:
        service = await _service(runtime, store)
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        initialized = await client.initialize()
        assert initialized.capabilities.item_deltas
        thread = await client.create_thread(str(tmp_path), request_id="create-stream")
        cursor = thread.cursor
        await client.start_turn(thread.thread_id, "输出文本", request_id="turn-stream")

        seen_delta = False
        seen_terminal = False
        for _ in range(100):
            page = await client.next_events(
                thread.thread_id,
                after_cursor=cursor,
                wait_ms=100,
                limit=100,
            )
            cursor = max(cursor, page.replay.scanned_through)
            seen_delta = seen_delta or any(delta.delta == "流式文本" for delta in page.deltas)
            seen_terminal = seen_terminal or any(
                event.turn_id is not None
                and event.data.type == "turn_state_changed"
                and event.data.status == "completed"
                for event in page.replay.events
            )
            if seen_delta and seen_terminal:
                break
        assert seen_delta and seen_terminal
        timed_out = await client.next_events(
            thread.thread_id,
            after_cursor=cursor,
            wait_ms=0,
        )
        assert timed_out.timed_out
        replay = await client.replay_events(thread.thread_id, after_cursor=0, limit=100)
        assert replay.scanned_through == cursor
        await client.close()


async def test_events_next_returns_progress_arriving_at_timeout_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        service = await _service(runtime, store)
        progressed = EventsNextResult(
            replay=EventsReplayResult(
                thread_id=thread.thread_id,
                events=(),
                scanned_through=thread.sequence + 1,
                has_more=False,
            )
        )
        snapshot = AsyncMock(side_effect=[None, progressed])
        monkeypatch.setattr(service, "_next_snapshot", snapshot)
        result = await service.next_events(
            EventsNextParams(
                thread_id=thread.thread_id,
                after_cursor=thread.sequence,
                wait_ms=0,
            )
        )
        assert result == progressed and not result.timed_out
        assert snapshot.await_count == 2
        await service.close()


async def test_events_next_marks_bounded_delta_buffer_gap(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        cursor = (await store.events(thread.thread_id))[-1].sequence
        service = await _service(runtime, store)
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        turn_id = uuid4()
        item_id = uuid4()
        for sequence in range(1, 1003):
            service._receive_delta(
                ItemDelta(
                    thread_id=thread.thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    model_step=1,
                    stream_sequence=sequence,
                    delta=str(sequence),
                )
            )

        first = await client.next_events(
            thread.thread_id,
            after_cursor=cursor,
            wait_ms=0,
            limit=10,
        )
        assert first.live_gap
        assert first.live_has_more
        assert [delta.stream_sequence for delta in first.deltas] == list(range(3, 13))
        second = await client.next_events(
            thread.thread_id,
            after_cursor=cursor,
            wait_ms=0,
            limit=10,
        )
        assert not second.live_gap
        assert second.live_has_more
        assert [delta.stream_sequence for delta in second.deltas] == list(range(13, 23))
        await client.close()


async def test_events_next_omits_deltas_when_client_did_not_negotiate_them(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        cursor = (await store.events(thread.thread_id))[-1].sequence
        service = await _service(runtime, store)
        server = AgentProtocolServer(service)
        await server.process_frame(
            _request(
                "initialize",
                {
                    "protocolVersion": "1.0",
                    "clientInfo": {"name": "no-delta", "version": "1"},
                    "clientInstanceId": str(uuid4()),
                },
            )
        )
        await server.process_frame(
            b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        )
        service._receive_delta(
            ItemDelta(
                thread_id=thread.thread_id,
                turn_id=uuid4(),
                item_id=uuid4(),
                model_step=1,
                stream_sequence=1,
                delta="不应下发",
            )
        )

        response = _decoded(
            await server.process_frame(
                _request(
                    "events/next",
                    EventsNextParams(
                        thread_id=thread.thread_id,
                        after_cursor=cursor,
                        wait_ms=0,
                    ).model_dump(mode="json", by_alias=True),
                    request_id=2,
                )
            )
        )
        result = response["result"]
        assert isinstance(result, dict)
        assert result["deltas"] == []
        assert result["timedOut"] is True
        await server.close()


async def test_sdk_approval_response_drives_decided_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer("审批后完成")])
    async with AgentRuntime(store, provider, tools) as runtime:
        service = await _service(runtime, store)
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create-approval")
        accepted = await client.start_turn(thread.thread_id, "读取文件", request_id="turn-approval")
        await wait_for_turn_status(client, thread.thread_id, "waiting_approval")
        internal = await store.get_thread(thread.thread_id)
        turn = internal.turns[-1]
        call = next(
            item.content
            for item in turn.items
            if getattr(item.content, "kind", None) == "tool_call"
        )
        approval = approval_for(turn, call)
        assert approval is not None
        content = approval.content
        responded = await client.respond_approval(
            ApprovalRespondParams(
                request_id="approve",
                thread_id=thread.thread_id,
                turn_id=accepted.turn_id,
                approval_id=content.approval_id,
                fingerprint=content.request_fingerprint,
                decision=PublicApprovalDecision(outcome="approved", actor="cli-user"),
            )
        )
        assert responded.status == "waiting_approval"
        completed = await wait_for_turn_status(client, thread.thread_id, "completed")
        await service.close()
        assert completed.latest_turn is not None
        assert completed.latest_turn.status == "completed"
        assert len(tools.calls) == 1
        await client.close()


async def test_artifact_read_is_advertised_only_with_scoped_reader(tmp_path: Path) -> None:
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=300)
    reference = results(turn)[0].output["artifact"]
    async with CodingToolRuntime(tmp_path / "repo", artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            FakeProvider(),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            service = AgentApplicationService(
                runtime,
                store,
                SQLiteProtocolRequestStore(store.path),
                ScopedProtocolArtifactReader(store, artifacts, tools),
            )
            client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
            initialized = await client.initialize()
            assert initialized.capabilities.artifact_pages
            assert "artifact/read" in initialized.capabilities.methods
            page = await client.read_artifact(
                thread.thread_id,
                UUID(reference["artifact_id"]),
                limit=10,
            )
            assert page.artifact.records == 300
            assert len(page.text.splitlines()) == 10
            assert page.next_offset == 10
            await client.close()
