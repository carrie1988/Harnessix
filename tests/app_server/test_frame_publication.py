"""原封套、完整响应、握手候选及实际stdio出口的版本材料保护。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from urllib.parse import quote
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer, ConnectionState
from harnessix.app_server.service import AgentApplicationService, AgentServiceError
from harnessix.app_server.stdio import run_stdio
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import ProtocolLimits
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY, protected


def frame(method, params, identity=17):
    return (
        json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}) + "\n"
    ).encode()


def initialization(client=None):
    return {
        "protocolVersion": "2.0",
        "clientInfo": {"name": "test", "version": "1"},
        "clientInstanceId": str(client or uuid4()),
    }


def server_for(runtime):
    return AgentProtocolServer(
        AgentApplicationService(
            runtime, runtime.store, SQLiteProtocolRequestStore(runtime.store.path)
        )
    )


async def ready(server):
    response = json.loads((await server.process_frame(frame("initialize", initialization())))[0])
    assert "result" in response
    assert (
        await server.process_frame(
            b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        )
        == ()
    )
    assert server.state == ConnectionState.READY


@pytest.mark.parametrize(
    "surface",
    ["id", "method", "param_value", "param_key", "client_name", "nested_key", "list_value"],
)
@pytest.mark.parametrize("encoding", ["raw", "base64", "url"])
async def test_complete_envelope_rejection_before_handshake_or_any_command(
    tmp_path, surface, encoding
):
    value = {
        "raw": CANARY,
        "base64": base64.b64encode(CANARY.encode()).decode(),
        "url": quote(CANARY, safe=""),
    }[encoding]
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            original = await runtime.create_thread(str(tmp_path))
            server = server_for(runtime)
            params = initialization()
            if surface == "param_value":
                params["unknown"] = value
            if surface == "param_key":
                params[value] = "safe"
            if surface == "client_name":
                params["clientInfo"]["name"] = value
            if surface == "nested_key":
                params["unknown"] = {value: "safe"}
            if surface == "list_value":
                params["unknown"] = ["safe", value]
            limits = server.limits
            responses = await server.process_frame(
                frame(
                    value if surface == "method" else "initialize",
                    params,
                    value if surface == "id" else 17,
                )
            )
            dto = json.loads(responses[0])
            assert dto["error"]["data"]["code"] == "public_input_secret_leak"
            assert dto["id"] == (None if surface == "id" else 17)
            assert CANARY.encode() not in responses[0] and value.encode() not in responses[0]
            assert server.state == ConnectionState.NEW and server.client_instance_id is None
            assert server.limits == limits and server.item_deltas_enabled is False
            assert await runtime.store.get_thread(original.thread_id) == original
            await server.close()


@pytest.mark.parametrize("surface", ["method", "params", "key"])
async def test_rejected_notification_never_acknowledges_or_emits_response(tmp_path, surface):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            await server.process_frame(frame("initialize", initialization()))
            message = {
                "jsonrpc": "2.0",
                "method": CANARY if surface == "method" else "notifications/initialized",
                "params": {},
            }
            if surface == "params":
                message["params"]["value"] = CANARY
            if surface == "key":
                message["params"][CANARY] = "safe"
            assert await server.process_frame((json.dumps(message) + "\n").encode()) == ()
            assert server.state == ConnectionState.INITIALIZED_PENDING_ACK
            await server.close()


@pytest.mark.parametrize(
    "method", ["thread/get", "thread/list", "thread/resume", "events/replay", "events/next"]
)
async def test_current_registered_legacy_query_value_does_not_reach_transport_or_modify_history(
    tmp_path, method
):
    path = tmp_path / "s.db"
    async with AgentRuntime(
        SQLiteSessionStore(path), ScriptedProvider([answer(CANARY)])
    ) as runtime:
        legacy = await runtime.create_thread(str(tmp_path / CANARY))
        await runtime.run_turn(legacy.thread_id, "safe", request_id="legacy")
    with protected() as scope:
        provider = ScriptedProvider([])
        async with AgentRuntime(
            SQLiteSessionStore(path), provider, public_output_protection=scope
        ) as runtime:
            server = server_for(runtime)
            await ready(server)
            before = await runtime.store.get_thread(legacy.thread_id)
            params = {} if method == "thread/list" else {"threadId": str(legacy.thread_id)}
            if method == "events/next":
                params["waitMs"] = 0
            response = (await server.process_frame(frame(method, params)))[0]
            dto = json.loads(response)
            assert dto["id"] == 17 and dto["error"]["data"]["code"] == "public_output_secret_leak"
            assert CANARY.encode() not in response
            assert (
                await runtime.store.get_thread(legacy.thread_id) == before and not provider.requests
            )
            # 已有私有历史不清洗、不改Hash；有限材料拒绝不能冒充历史授权。
            assert CANARY in before.model_dump_json()
            await server.close()


@pytest.mark.parametrize("error_type", ["kernel", "service", "unexpected"])
async def test_extension_error_code_message_and_diagnostics_are_checked_before_response(
    tmp_path, monkeypatch, error_type
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            await ready(server)

            async def broken(*args):
                if error_type == "kernel":
                    raise KernelError(CANARY, CANARY)
                if error_type == "service":
                    raise AgentServiceError(CANARY, CANARY)
                raise RuntimeError(CANARY)

            monkeypatch.setattr(server, "_dispatch", broken)
            response = (await server.process_frame(frame("thread/list", {})))[0]
            dto = json.loads(response)
            assert dto["id"] == 17 and CANARY.encode() not in response
            assert dto["error"]["data"]["code"] == (
                "internal_error" if error_type == "unexpected" else "public_output_secret_leak"
            )
            await server.close()


@pytest.mark.parametrize("fault", ["registered_version", "extension_exception"])
async def test_handshake_metadata_or_constructor_failure_never_commits_candidate(
    tmp_path, monkeypatch, fault
):
    from harnessix.app_server import server as module

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            original_limits = server.limits

            def version():
                if fault == "extension_exception":
                    raise RuntimeError(CANARY)
                return CANARY

            monkeypatch.setattr(module, "_product_version", version)
            params = initialization()
            params["limits"] = {
                "maxMessageBytes": 4096,
                "maxPendingRequests": 1,
                "maxOutboundMessages": 8,
                "maxReplayEvents": 1,
            }
            params["capabilities"] = {"itemDeltas": True}
            response = (await server.process_frame(frame("initialize", params)))[0]
            dto = json.loads(response)
            assert CANARY.encode() not in response
            assert dto["error"]["data"]["code"] == (
                "internal_error" if fault == "extension_exception" else "public_output_secret_leak"
            )
            assert server.state == ConnectionState.NEW and server.client_instance_id is None
            assert server.item_deltas_enabled is False and server.limits == original_limits
            monkeypatch.setattr(module, "_product_version", lambda: "1.0")
            assert "result" in json.loads(
                (await server.process_frame(frame("initialize", params)))[0]
            )
            assert server.limits.max_pending_requests == 1 and server.item_deltas_enabled is True
            await server.close()


@pytest.mark.parametrize("mode", ["cancel", "close", "concurrent"])
async def test_handshake_publication_window_has_cancel_close_and_concurrent_cas(
    tmp_path, monkeypatch, mode
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            original_limits = server.limits
            original = runtime.validate_public_frame
            reached = asyncio.Event()
            both_reached = asyncio.Event()
            release = asyncio.Event()
            count = [0]

            async def gated(body):
                if b'"result"' in body:
                    count[0] += 1
                    reached.set()
                    if count[0] == 2:
                        both_reached.set()
                    await release.wait()
                await original(body)

            monkeypatch.setattr(runtime, "validate_public_frame", gated)
            first_client = uuid4()
            first = asyncio.create_task(
                server.process_frame(frame("initialize", initialization(first_client), 1))
            )
            await asyncio.wait_for(reached.wait(), 2)
            if mode == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert server.state == ConnectionState.NEW and server.client_instance_id is None
                assert server.limits == original_limits
            elif mode == "close":
                await server.close()
                release.set()
                dto = json.loads((await first)[0])
                assert dto["error"]["data"]["code"] == "server_closing" and dto["id"] is None
                assert server.state == ConnectionState.CLOSED and server.client_instance_id is None
            else:
                second_client = uuid4()
                second = asyncio.create_task(
                    server.process_frame(frame("initialize", initialization(second_client), 2))
                )
                await asyncio.wait_for(both_reached.wait(), 2)
                release.set()
                dtos = [json.loads(result[0]) for result in await asyncio.gather(first, second)]
                assert sum("result" in dto for dto in dtos) == 1
                assert (
                    sum(
                        dto.get("error", {}).get("data", {}).get("code") == "already_initialized"
                        for dto in dtos
                    )
                    == 1
                )
                winner = first_client if "result" in dtos[0] else second_client
                assert (
                    server.client_instance_id == winner
                    and server.state == ConnectionState.INITIALIZED_PENDING_ACK
                )
            await server.close()


async def test_frame_failure_after_completed_command_preserves_receipt_and_does_not_reexecute(
    tmp_path, monkeypatch
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            await ready(server)
            original = runtime.validate_public_frame

            async def broken(body):
                if b"harnessix.agent-protocol-thread/v1" in body:
                    raise KernelError("public_output_secret_leak", CANARY)
                await original(body)

            monkeypatch.setattr(runtime, "validate_public_frame", broken)
            params = {"requestId": "same", "workspace": str(tmp_path)}
            response = (await server.process_frame(frame("thread/create", params)))[0]
            assert json.loads(response)["error"]["data"]["code"] == "public_output_secret_leak"
            assert CANARY.encode() not in response
            record = await server.service.requests.get(server.client_instance_id, "same")
            assert record.state == "completed"
            selected, _ = await runtime.store.list_thread_page(after=None, archived=None, limit=200)
            assert len(selected) == 1
            monkeypatch.setattr(runtime, "validate_public_frame", original)
            repeated = json.loads((await server.process_frame(frame("thread/create", params)))[0])
            assert repeated["result"] == record.outcome
            assert await server.service.requests.get(server.client_instance_id, "same") == record
            assert await runtime.store.list_thread_page(after=None, archived=None, limit=200) == (
                selected,
                False,
            )
            await server.close()


@pytest.mark.parametrize("stage", ["input", "output"])
@pytest.mark.parametrize("mode", ["closed", "limit", "timeout", "parent", "extension"])
async def test_frame_guard_failure_and_cancel_leave_handshake_uncommitted(
    tmp_path, monkeypatch, stage, mode
):
    from harnessix.agent import publication as boundary
    from harnessix.secrets import publication

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            if stage == "input" and mode == "closed":
                scope.close()
            original_jsonl = scope.assert_public_jsonl
            original_step = publication._ScanBudget.step
            clock = [boundary.monotonic()]
            monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])

            def fault():
                if mode == "limit":
                    monkeypatch.setattr(publication, "MAX_SCAN_WORK", 2)
                if mode == "timeout":
                    clock[0] += 20
                if mode == "parent":
                    asyncio.current_task().cancel()
                if mode == "closed":
                    scope.close()
                if mode == "extension":
                    raise RuntimeError(CANARY)

            def step(work, depth):
                if stage == "input":
                    fault()
                original_step(work, depth)

            def jsonl(body, *, checkpoint):
                if stage == "output":
                    fault()
                original_jsonl(body, checkpoint=checkpoint)

            monkeypatch.setattr(publication._ScanBudget, "step", step)
            monkeypatch.setattr(scope, "assert_public_jsonl", jsonl)
            task = asyncio.create_task(server.process_frame(frame("initialize", initialization())))
            if mode == "parent":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = (await task)[0]
                dto = json.loads(response)
                suffix = {
                    "closed": "secret_unavailable",
                    "limit": "limit",
                    "timeout": "timeout",
                    "extension": "protection_failed",
                }[mode]
                assert dto["error"]["data"]["code"] == "public_" + stage + "_" + suffix
                assert CANARY.encode() not in response
            assert server.state == ConnectionState.NEW and server.client_instance_id is None
            await server.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","id":"' + CANARY.encode() + b'","params":',
        b"\xff",
        b'{"jsonrpc":"2.0","id":"' + CANARY.encode() + b'","id":1,"method":"initialize"}',
    ],
)
async def test_malformed_control_frames_have_no_original_identity_or_error_path(tmp_path, raw):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            response = (await server.process_frame(raw))[0]
            dto = json.loads(response)
            assert dto["id"] is None and dto["error"]["data"]["path"] == []
            assert CANARY.encode() not in response and server.state == ConnectionState.NEW
            await server.close()


async def test_actual_stdio_writer_never_receives_raw_sensitive_metadata_or_replay(tmp_path):
    path = tmp_path / "s.db"
    async with AgentRuntime(
        SQLiteSessionStore(path), ScriptedProvider([answer(CANARY)])
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "safe", request_id="legacy")
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            server = server_for(runtime)
            incoming = io.BytesIO(
                frame("initialize", initialization(), CANARY)
                + frame("initialize", initialization(), 1)
                + b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
                + frame("events/replay", {"threadId": str(thread.thread_id)}, 2)
            )
            outgoing = io.BytesIO()
            await run_stdio(server, incoming, outgoing)
            body = outgoing.getvalue()
            assert CANARY.encode() not in body
            dtos = [json.loads(line) for line in body.splitlines()]
            assert len(dtos) == 3 and dtos[0]["id"] is None and "result" in dtos[1]
            assert dtos[2]["error"]["data"]["code"] == "public_output_secret_leak"
            assert server.state == ConnectionState.CLOSED


async def test_safe_encoding_handshake_and_queries_keep_original_wire_and_correlation(tmp_path):
    async def exercise(guard):
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / ("guard.db" if guard else "plain.db")),
            ScriptedProvider([]),
            public_output_protection=guard,
        ) as runtime:
            server = server_for(runtime)
            reply = (
                await server.process_frame(frame("initialize", initialization(), "中文相关ID"))
            )[0]
            assert json.loads(reply)["id"] == "中文相关ID"
            await server.process_frame(
                b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
            )
            empty = (await server.process_frame(frame("thread/list", {}, 18)))[0]
            await server.close()
            return reply, empty

    with protected() as scope:
        guarded = await exercise(scope)
    plain = await exercise(None)
    assert guarded == plain


async def test_response_error_path_is_checked_even_if_extension_constructs_it(
    tmp_path, monkeypatch
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            await ready(server)

            async def response(message):
                return server._error(
                    message.id, -32602, "invalid_params", "参数无效", path=(CANARY,)
                )

            monkeypatch.setattr(server, "_handle_request", response)
            result = (await server.process_frame(frame("thread/list", {})))[0]
            assert CANARY.encode() not in result
            dto = json.loads(result)
            assert dto["id"] == 17 and dto["error"]["data"]["code"] == "public_output_secret_leak"
            assert dto["error"]["data"]["path"] == []
            await server.close()


async def test_unknown_historical_material_remains_unproven_not_authorized_by_current_scope(
    tmp_path,
):
    from harnessix.product_config.contracts import SecretReference
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope

    path = tmp_path / "s.db"
    async with AgentRuntime(
        SQLiteSessionStore(path), ScriptedProvider([answer(CANARY)])
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "safe", request_id="legacy")
    source = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "10", "KEY"),),
        environment={"KEY": "different-fixture-value/+10"},
    )
    with SecretPublicationScope((SecretReference(name="api", version="10"),), source) as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            server = server_for(runtime)
            await ready(server)
            before = await runtime.store.get_thread(thread.thread_id)
            result = (
                await server.process_frame(
                    frame("events/replay", {"threadId": str(thread.thread_id)})
                )
            )[0]
            # 该已知缺口观察证明当前材料检查不是历史Seal，不计作历史安全成功。
            assert "result" in json.loads(result) and CANARY.encode() in result
            assert await runtime.store.get_thread(thread.thread_id) == before
            await server.close()


@pytest.mark.parametrize("size", [4095, 4096, 4097])
async def test_exact_outbound_utf8_limit_counts_original_frame_bytes(tmp_path, size):
    from harnessix.app_server.frame_publication import encode, publish_frame
    from harnessix.protocol.contracts import JsonRpcSuccessResponse

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            fixed = encode(JsonRpcSuccessResponse(id=17, result={"text": "中文"}))
            original = encode(
                JsonRpcSuccessResponse(id=17, result={"text": "中文" + "x" * (size - len(fixed))})
            )
            assert len(original) == size
            published, accepted = await publish_frame(runtime, original, 17, max_message_bytes=4096)
            assert len(published) <= 4096
            if size <= 4096:
                assert accepted and published == original
            else:
                assert (
                    not accepted
                    and json.loads(published)["error"]["data"]["code"] == "response_too_large"
                )


async def test_oversize_replay_preserves_negotiated_limit_and_history_and_can_page_smaller(
    tmp_path,
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([answer("x" * 2000)]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            await runtime.run_turn(thread.thread_id, "safe", request_id="large")
            server = server_for(runtime)
            server.limits = ProtocolLimits(max_message_bytes=4096)
            await ready(server)
            before = await runtime.store.get_thread(thread.thread_id)
            rejected = (
                await server.process_frame(
                    frame("events/replay", {"threadId": str(thread.thread_id)})
                )
            )[0]
            assert json.loads(rejected)["error"]["data"]["code"] == "response_too_large"
            assert len(rejected) <= server.limits.max_message_bytes
            smaller = (
                await server.process_frame(
                    frame("events/replay", {"threadId": str(thread.thread_id), "limit": 1})
                )
            )[0]
            assert (
                "result" in json.loads(smaller) and len(smaller) <= server.limits.max_message_bytes
            )
            assert await runtime.store.get_thread(thread.thread_id) == before
            await server.close()


@pytest.mark.parametrize("state", [ConnectionState.CLOSING, ConnectionState.CLOSED])
async def test_closed_server_ignores_notifications_and_uses_fixed_null_request_error(
    tmp_path, state
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            server = server_for(runtime)
            server.state = state
            scope.close()
            notification = (
                json.dumps({"jsonrpc": "2.0", "method": CANARY, "params": {CANARY: CANARY}}) + "\n"
            ).encode()
            assert await server.process_frame(notification) == ()
            result = (await server.process_frame(frame("thread/list", {}, CANARY)))[0]
            dto = json.loads(result)
            assert dto["id"] is None and dto["error"]["data"]["code"] == "server_closing"
            assert CANARY.encode() not in result and server.state == state
            await server.close()
