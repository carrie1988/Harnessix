from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
from collections.abc import Buffer
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer, ConnectionState
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.models.contracts import ResponseCompleted, ResponseStarted
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.protocol.contracts import ThreadCreateParams, TurnResult, TurnStartParams
from harnessix.protocol.projection import project_turn
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import (
    AgentClient,
    InProcessAgentTransport,
    SubprocessAgentTransport,
)
from harnessix.session.sqlite import SQLiteSessionStore


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
        assert server.state.value == ConnectionState.INITIALIZED_PENDING_ACK.value
        assert (
            await server.process_frame(
                b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
            )
            == ()
        )
        assert server.state.value == ConnectionState.READY.value


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
        await service.close()
        current = await client.get_thread(params.thread_id)
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
            await asyncio.wait_for(task, timeout=1)
        finally:
            outgoing.release.set()
        assert server.state is ConnectionState.CLOSED
        assert await store.thread_ids() == []
