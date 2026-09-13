from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.product_ui import (
    ClientStateStore,
    ConnectionPhase,
    PreparedClientCommand,
    ProductUIError,
    RecoverableAgentSession,
)
from harnessix.protocol.contracts import (
    EventsNextResult,
    EventsReplayResult,
    InitializeResult,
    ItemPublicEvent,
    JsonRpcNotification,
    ProtocolLimits,
    PublicEvent,
    PublicItem,
    PublicTextContent,
    ServerCapabilities,
    ServerInfo,
    ThreadResult,
    ThreadView,
)
from harnessix.sdk import AgentClient, AgentSDKError

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _SessionTransport:
    def __init__(self, thread: ThreadView, event: PublicEvent, *, warm_cursor: int) -> None:
        self.thread = thread
        self.event = event
        self.warm_cursor = warm_cursor
        self.requests: list[dict[str, object]] = []
        self.notifications: list[JsonRpcNotification] = []
        self.closed = False

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        request = json.loads(frame)
        self.requests.append(request)
        method = request["method"]
        if method == "initialize":
            result = InitializeResult(
                server_info=ServerInfo(version="0.9.1-test"),
                capabilities=ServerCapabilities(
                    methods=("events/next", "events/replay", "thread/get")
                ),
                limits=ProtocolLimits(max_replay_events=16),
            )
        elif method == "thread/get":
            result = ThreadResult(thread=self.thread)
        elif method == "events/replay":
            after_cursor = request["params"]["afterCursor"]
            result = (
                EventsReplayResult(
                    thread_id=self.thread.thread_id,
                    events=(self.event,),
                    scanned_through=5,
                    has_more=False,
                )
                if after_cursor == 0
                else EventsReplayResult(
                    thread_id=self.thread.thread_id,
                    events=(),
                    scanned_through=self.warm_cursor,
                    has_more=False,
                )
            )
        elif method == "events/next":
            result = EventsNextResult(
                replay=EventsReplayResult(
                    thread_id=uuid4(),
                    events=(),
                    scanned_through=self.warm_cursor,
                    has_more=False,
                ),
                deltas=(),
                live_gap=False,
            )
        else:
            raise AssertionError(f"unexpected method: {method}")
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": result.model_dump(mode="json", by_alias=True),
        }
        return (json.dumps(response, separators=(",", ":")).encode() + b"\n",)

    async def notify(self, frame: bytes) -> None:
        self.notifications.append(JsonRpcNotification.model_validate_json(frame))

    async def close(self) -> None:
        self.closed = True


def _thread(thread_id: UUID) -> ThreadView:
    return ThreadView(
        thread_id=thread_id,
        workspace="/workspace",
        cursor=6,
        turn_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _event(thread_id: UUID, item_id: UUID) -> PublicEvent:
    return PublicEvent(
        event_id=uuid4(),
        thread_id=thread_id,
        turn_id=uuid4(),
        cursor=3,
        occurred_at=NOW,
        data=ItemPublicEvent(
            type="item_finished",
            item=PublicItem(
                item_id=item_id,
                status="completed",
                content=PublicTextContent(kind="assistant_message", text="历史正文"),
            ),
        ),
    )


async def test_session_cold_replays_from_zero_and_warm_reconnect_resumes_projection(
    tmp_path: Path,
) -> None:
    thread_id = uuid4()
    item_id = uuid4()
    transports: list[_SessionTransport] = []

    def factory() -> _SessionTransport:
        thread = _thread(thread_id).model_copy(update={"cursor": 5 + len(transports)})
        transport = _SessionTransport(thread, _event(thread_id, item_id), warm_cursor=6)
        transports.append(transport)
        return transport

    with ClientStateStore(tmp_path / "state", workspace_identity="/workspace") as store:
        store.advance_cursor(thread_id, 5)
        session = RecoverableAgentSession(store, factory)
        first_connection = await session.connect()
        cold = await session.hydrate_thread(thread_id)

        replay_requests = [
            item for item in transports[0].requests if item["method"] == "events/replay"
        ]
        assert replay_requests[0]["params"]["afterCursor"] == 0  # type: ignore[index]
        assert cold.durable_cursor == 5
        assert cold.item_for(item_id) is not None
        assert store.state().selected_thread_id == thread_id

        second_connection = await session.connect()
        warm = await session.hydrate_thread(thread_id)
        replay_requests = [
            item for item in transports[1].requests if item["method"] == "events/replay"
        ]
        assert replay_requests[0]["params"]["afterCursor"] == 5  # type: ignore[index]
        assert warm.durable_cursor == 6
        assert warm.item_for(item_id) is not None
        assert second_connection.generation == first_connection.generation + 1
        assert transports[0].closed

        await session.close()
        assert session.connection.phase is ConnectionPhase.CLOSED
        assert store.state().clean_shutdown


async def test_prepared_command_reuses_identity_after_ambiguous_connection_failure(
    tmp_path: Path,
) -> None:
    thread_id = uuid4()
    transports: list[_SessionTransport] = []

    def factory() -> _SessionTransport:
        transport = _SessionTransport(_thread(thread_id), _event(thread_id, uuid4()), warm_cursor=6)
        transports.append(transport)
        return transport

    with ClientStateStore(tmp_path / "state", workspace_identity="/workspace") as store:
        session = RecoverableAgentSession(store, factory)
        await session.connect()
        command = session.prepare_command()
        observed: list[str] = []

        async def ambiguous(_client: object, prepared: PreparedClientCommand) -> None:
            observed.append(prepared.request_id)
            raise AgentSDKError("server_closed", "连接在结果返回前关闭", retryable=True)

        with pytest.raises(AgentSDKError):
            await session.execute_prepared(command, ambiguous)
        assert session.connection.phase is ConnectionPhase.BROKEN

        await session.connect()

        async def succeeds(_client: object, prepared: PreparedClientCommand) -> str:
            observed.append(prepared.request_id)
            return prepared.request_id

        assert await session.execute_prepared(command, succeeds) == command.request_id
        assert observed == [command.request_id, command.request_id]
        assert store.state().next_command_sequence == 2
        await session.close()


async def test_read_only_query_does_not_allocate_command_and_marks_transport_failure(
    tmp_path: Path,
) -> None:
    thread_id = uuid4()
    with ClientStateStore(tmp_path / "state", workspace_identity="/workspace") as store:
        session = RecoverableAgentSession(
            store,
            lambda: _SessionTransport(
                _thread(thread_id), _event(thread_id, uuid4()), warm_cursor=6
            ),
        )
        await session.connect()
        before = store.state().next_command_sequence

        async def read(client: AgentClient) -> str:
            assert client is not None
            return "只读结果"

        assert await session.execute_query(read) == "只读结果"
        assert store.state().next_command_sequence == before

        async def disconnected(_client: AgentClient) -> None:
            raise AgentSDKError("server_closed", "连接关闭", retryable=True)

        with pytest.raises(AgentSDKError):
            await session.execute_query(disconnected)
        assert session.connection.phase is ConnectionPhase.BROKEN
        assert store.state().next_command_sequence == before
        await session.close()


async def test_poll_projection_failure_marks_connection_broken(tmp_path: Path) -> None:
    thread_id = uuid4()
    thread = _thread(thread_id).model_copy(update={"cursor": 5})

    with ClientStateStore(tmp_path / "state", workspace_identity="/workspace") as store:
        session = RecoverableAgentSession(
            store,
            lambda: _SessionTransport(thread, _event(thread_id, uuid4()), warm_cursor=5),
        )
        await session.connect()
        await session.hydrate_thread(thread_id)

        with pytest.raises(ProductUIError) as error:
            await session.poll_thread(thread_id, wait_ms=0)

        assert error.value.code == "projection_thread_mismatch"
        assert session.connection.phase is ConnectionPhase.BROKEN
