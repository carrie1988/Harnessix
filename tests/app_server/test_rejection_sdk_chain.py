"""真实Adapter/认证SQLite/协议/SDK/UI闭合链；线流离线固定，不计真实编码质量。"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx

from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import FakeProvider
from harnessix.product_ui.projection import apply_replay_page, cold_product_view
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import ItemPublicEvent, PublicToolCallRejectionContent
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from tests.agent.helpers import RecordingTools
from tests.agent.test_publication import protected
from tests.helpers import wait_for_turn_status
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames
from tests.session.test_authenticated_history import KEY, read


async def test_authenticated_adapter_rejection_correction_replay_and_ui(tmp_path):
    unknown = call(arguments='{"私有":"PRIVATE-ARGUMENT-CANARY"}')
    unknown["function"]["name"] = "PRIVATE-NAME-CANARY"
    streams = [
        WireStream(
            [
                frame(chunk({"tool_calls": [unknown]})),
                frame(chunk(finish="tool_calls")),
                frame(chunk(usage=True)),
                b"data: [DONE]\n\n",
            ]
        ),
        WireStream(tool_frames()),
        WireStream(text_frames()),
    ]
    requests, tools = [], RecordingTools()

    def handle(request):
        if len(requests) == 1:
            assert tools.calls == []
        requests.append(json.loads(request.content))
        return response(streams[len(requests) - 1])

    with protected() as scope:
        proof = SessionPublicationBinding(uuid4(), uuid4(), KEY, scope)
        try:
            store = SQLiteSessionStore(tmp_path / "sdk.db", publication=proof)
            async with (
                OpenAIChatProvider(
                    OpenAIChatConfig(model="test-model", max_attempts=1),
                    api_key="fixture-key",
                    transport=httpx.MockTransport(handle),
                ) as provider,
                AgentRuntime(store, provider, tools, public_output_protection=scope) as runtime,
            ):
                service = AgentApplicationService(
                    runtime, store, SQLiteProtocolRequestStore(store.path)
                )
                client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
                assert (await client.initialize()).protocol_version == "2.0"
                thread = await client.create_thread(str(tmp_path), request_id="create")
                accepted = await client.start_turn(
                    thread.thread_id, "纠正后完成任务", request_id="turn"
                )
                await wait_for_turn_status(client, thread.thread_id, "completed")
                duplicate = await client.start_turn(
                    thread.thread_id, "纠正后完成任务", request_id="turn"
                )
                assert duplicate.turn_id == accepted.turn_id
                page = await client.replay_events(thread.thread_id)
                live = await client.next_events(thread.thread_id, wait_ms=0)
                assert live.replay == page
                assert all(
                    e.spec_version == "harnessix.agent-protocol-event/v2" for e in page.events
                )
                public_rejects = [
                    e.data.item
                    for e in page.events
                    if isinstance(e.data, ItemPublicEvent)
                    and isinstance(e.data.item.content, PublicToolCallRejectionContent)
                ]
                assert len(public_rejects) == 2
                content = public_rejects[-1].content.model_dump(mode="json", by_alias=True)
                assert set(content) == {"kind", "callId", "reason", "modelStep"}
                view = apply_replay_page(cold_product_view(thread), page)
                assert any(line.role == "工具调用拒绝" for line in transcript_lines(view))
                assert apply_replay_page(view, page) == view
                await client.close()
            frame_history = await read(store, thread.thread_id)
            assert frame_history.thread == replay(frame_history.events)
            assert frame_history.thread.turns[-1].model_steps == len(requests) == 3
            assert len(tools.calls) == 1 and all(s.closed for s in streams)
            for private in ["PRIVATE-NAME-CANARY", "PRIVATE-ARGUMENT-CANARY"]:
                assert private not in repr(requests)
                assert private not in page.model_dump_json()
                assert private not in frame_history.thread.model_dump_json()
            marker = "harnessix_rejected_tool_v1"
            assistant = next(m for m in requests[1]["messages"] if m.get("tool_calls"))
            assert assistant["tool_calls"][0]["function"] == {
                "name": marker,
                "arguments": "{}",
            }
            assert not any(
                t["function"]["name"] == marker for request in requests for t in request["tools"]
            )
            # 新宿主只验证来源并读取；不得重发旧模型请求或执行继承调用。
            reopened_provider = FakeProvider()
            async with AgentRuntime(
                SQLiteSessionStore(store.path, publication=proof),
                reopened_provider,
                tools,
                public_output_protection=scope,
            ) as reopened:
                assert await reopened.store.get_thread(thread.thread_id) == frame_history.thread
            assert reopened_provider.requests == [] and len(tools.calls) == 1
        finally:
            proof.close()


async def test_truncated_tool_call_rejection_crosses_protocol_sdk_and_ui(tmp_path):
    # 开启参数修复后，截断的工具调用以invalid_arguments_json拒绝：公共投影、SDK与UI必须接受该原因，
    # 否则服务端投影失败、客户端退出并取消回合（holdout实测）。
    truncated = tool_frames('{"path": "PRIVATE-TRUNCATED-CANARY')
    truncated[1] = frame(chunk(finish="length"))
    streams = [WireStream(truncated), WireStream(text_frames())]
    tools = RecordingTools()
    with protected() as scope:
        proof = SessionPublicationBinding(uuid4(), uuid4(), KEY, scope)
        try:
            store = SQLiteSessionStore(tmp_path / "sdk.db", publication=proof)
            async with (
                OpenAIChatProvider(
                    OpenAIChatConfig(
                        model="test-model", max_attempts=1, repair_tool_arguments=True
                    ),
                    api_key="fixture-key",
                    transport=httpx.MockTransport(lambda _: response(streams.pop(0))),
                ) as provider,
                AgentRuntime(store, provider, tools, public_output_protection=scope) as runtime,
            ):
                service = AgentApplicationService(
                    runtime, store, SQLiteProtocolRequestStore(store.path)
                )
                client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
                await client.initialize()
                thread = await client.create_thread(str(tmp_path), request_id="create")
                await client.start_turn(thread.thread_id, "截断后重发", request_id="turn")
                await wait_for_turn_status(client, thread.thread_id, "completed")
                page = await client.replay_events(thread.thread_id)
                assert (await client.get_thread(thread.thread_id)).latest_turn.status == (
                    "completed"
                )
                reasons = {
                    e.data.item.content.reason
                    for e in page.events
                    if isinstance(e.data, ItemPublicEvent)
                    and isinstance(e.data.item.content, PublicToolCallRejectionContent)
                }
                assert reasons == {"invalid_arguments_json"}
                view = apply_replay_page(cold_product_view(thread), page)
                assert ("工具调用拒绝", "第 1 步：工具参数不是合法JSON，调用已拒绝") in [
                    (line.role, line.text) for line in transcript_lines(view)
                ]
                assert "PRIVATE-TRUNCATED-CANARY" not in page.model_dump_json()
                await client.close()
            assert tools.calls == [] and not streams
        finally:
            proof.close()
