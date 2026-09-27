"""导出的Application Service直调查询保护；真实SQLite，合成材料，无网络模型。"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemDelta
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.service import AgentApplicationService, AgentServiceError
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import (
    ArtifactReadParams,
    EventsNextParams,
    EventsReplayParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadResumeParams,
)
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY, protected


def service_for(runtime, reader=None):
    return AgentApplicationService(
        runtime, runtime.store, SQLiteProtocolRequestStore(runtime.store.path), reader
    )


async def query(service, surface, identity):
    if surface == "get":
        return await service.get_thread(ThreadGetParams(thread_id=identity))
    if surface == "list":
        return await service.list_threads(ThreadListParams())
    if surface == "resume":
        return await service.resume_thread(ThreadResumeParams(thread_id=identity))
    if surface == "replay":
        return await service.replay_events(EventsReplayParams(thread_id=identity))
    return await service.next_events(EventsNextParams(thread_id=identity, wait_ms=0))


@pytest.mark.parametrize("surface", ["get", "list", "resume", "replay", "next"])
@pytest.mark.parametrize("encoded", [False, True])
async def test_direct_queries_reject_registered_legacy_projection_without_mutating_history(
    tmp_path, surface, encoded
):
    value = base64.b64encode(CANARY.encode()).decode() if encoded else CANARY
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([answer(value)])) as legacy:
        thread = await legacy.create_thread(str(tmp_path / value))
        await legacy.run_turn(thread.thread_id, "safe", request_id="legacy")
    with protected() as scope:
        provider = ScriptedProvider([])
        async with AgentRuntime(store, provider, public_output_protection=scope) as runtime:
            service = service_for(runtime)
            before = await store.get_thread(thread.thread_id)
            with pytest.raises(AgentServiceError) as caught:
                await query(service, surface, thread.thread_id)
            assert caught.value.code == "public_output_secret_leak"
            assert CANARY not in str(caught.value) and value not in str(caught.value)
            assert await store.get_thread(thread.thread_id) == before
            assert not service._tasks and not provider.requests
            await service.close()


@pytest.mark.parametrize("surface", ["get", "list", "resume", "replay", "next", "artifact"])
async def test_unavailable_input_guard_precedes_every_query_store_and_live_registration(
    tmp_path, monkeypatch, surface
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            service = service_for(runtime)
            read = AsyncMock(side_effect=AssertionError("查询拒绝前不允许Store访问"))
            monkeypatch.setattr(runtime.store, "get_thread", read)
            monkeypatch.setattr(runtime.store, "list_thread_page", read)
            monkeypatch.setattr(runtime.store, "events", read)
            scope.close()
            with pytest.raises(AgentServiceError) as caught:
                if surface == "artifact":
                    await service.read_artifact(
                        ArtifactReadParams(thread_id=thread.thread_id, artifact_id=uuid4())
                    )
                else:
                    await query(service, surface, thread.thread_id)
            assert caught.value.code == "public_input_secret_unavailable"
            assert not read.await_count and not service._delta_events and not service._tasks
            await service.close()


async def test_registered_query_cursor_rejected_before_store_access(tmp_path, monkeypatch):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            service = service_for(runtime)
            read = AsyncMock()
            monkeypatch.setattr(runtime.store, "list_thread_page", read)
            with pytest.raises(AgentServiceError) as caught:
                await service.list_threads(ThreadListParams(cursor=CANARY))
            assert caught.value.code == "public_input_secret_leak" and not read.await_count
            await service.close()


@pytest.mark.parametrize("surface", ["get", "list", "resume", "replay", "next"])
async def test_safe_direct_queries_preserve_original_dto_and_private_facts(tmp_path, surface):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([answer("安全原文")]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            await runtime.run_turn(thread.thread_id, "safe", request_id="same")
            before = await runtime.store.get_thread(thread.thread_id)
            service = service_for(runtime)
            result = await query(service, surface, thread.thread_id)
            assert str(thread.thread_id) in result.model_dump_json()
            assert CANARY not in result.model_dump_json()
            assert await runtime.store.get_thread(thread.thread_id) == before
            assert not service._tasks
            await service.close()


@pytest.mark.parametrize("kind", ["kernel_message", "kernel_code", "service", "unexpected"])
async def test_query_store_errors_cannot_export_registered_material(tmp_path, monkeypatch, kind):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            service = service_for(runtime)
            error = {
                "kernel_message": KernelError("storage_fixture", CANARY),
                "kernel_code": KernelError(CANARY, "safe"),
                "service": AgentServiceError("service_fixture", CANARY),
                "unexpected": ValueError(CANARY),
            }[kind]
            monkeypatch.setattr(runtime.store, "get_thread", AsyncMock(side_effect=error))
            with pytest.raises(AgentServiceError) as caught:
                await service.get_thread(ThreadGetParams(thread_id=thread.thread_id))
            assert caught.value.code == (
                "internal_error" if kind == "unexpected" else "public_output_secret_leak"
            )
            assert CANARY not in str(caught.value) and CANARY not in caught.value.code
            await service.close()


async def test_healthy_stable_query_error_retains_code_and_retryability(tmp_path, monkeypatch):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            service = service_for(runtime)
            monkeypatch.setattr(
                runtime.store,
                "get_thread",
                AsyncMock(
                    side_effect=KernelError("thread_not_found", "Thread不存在", retryable=True)
                ),
            )
            with pytest.raises(AgentServiceError) as caught:
                await service.get_thread(ThreadGetParams(thread_id=uuid4()))
            assert caught.value.code == "thread_not_found" and caught.value.retryable
            assert str(caught.value) == "Thread不存在"
            await service.close()


@pytest.mark.parametrize("encoding", ["raw", "base64"])
async def test_direct_live_delta_projection_is_checked_before_return(tmp_path, encoding):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            service = service_for(runtime)
            service._receive_delta(
                ItemDelta(
                    thread_id=thread.thread_id,
                    turn_id=uuid4(),
                    item_id=uuid4(),
                    model_step=1,
                    stream_sequence=1,
                    delta=CANARY
                    if encoding == "raw"
                    else base64.b64encode(CANARY.encode()).decode(),
                )
            )
            with pytest.raises(AgentServiceError) as caught:
                await service.next_events(
                    EventsNextParams(
                        thread_id=thread.thread_id, after_cursor=thread.sequence, wait_ms=0
                    )
                )
            assert caught.value.code == "public_output_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == thread
            await service.close()


@pytest.mark.parametrize("binding", ["different_store", "same_path_other_instance", "reader_store"])
async def test_query_host_binding_rejected_before_delta_subscription(
    tmp_path, monkeypatch, binding
):
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([])) as runtime:
        store = (
            SQLiteSessionStore(tmp_path / ("other.db" if binding == "different_store" else "s.db"))
            if binding != "reader_store"
            else runtime.store
        )
        reader = None
        if binding == "reader_store":
            other = SQLiteSessionStore(tmp_path / "other.db")
            artifacts = AsyncMock(session=other)
            reader = ScopedProtocolArtifactReader(other, artifacts, AsyncMock())
        subscribe = AsyncMock(side_effect=AssertionError("错误装配不得订阅"))
        monkeypatch.setattr(runtime, "subscribe_deltas", subscribe)
        with pytest.raises(ValueError) as caught:
            AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path), reader)
        assert "同一Session" in str(caught.value) and not subscribe.called


@pytest.mark.parametrize(
    "fault", ["registered_snapshot", "output_timeout", "parent_cancel", "service_close", "safe"]
)
async def test_resume_checks_public_snapshot_before_background_recovery_and_close_race(
    tmp_path, monkeypatch, fault
):
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([])) as legacy:
        thread = await legacy.create_thread(
            str(tmp_path / CANARY) if fault == "registered_snapshot" else str(tmp_path)
        )
        turn = await legacy.accept_turn(thread.thread_id, "safe", request_id="accepted")
    with protected() as scope:
        provider = ScriptedProvider([answer("安全恢复")])
        async with AgentRuntime(store, provider, public_output_protection=scope) as runtime:
            service = service_for(runtime)
            before = await store.get_thread(thread.thread_id)
            assert before.active_turn_id == turn.turn_id
            reached, release = asyncio.Event(), asyncio.Event()
            original = runtime.validate_public_output

            async def gate(value):
                if "thread" in value:
                    reached.set()
                    if fault == "output_timeout":
                        raise KernelError("public_output_timeout", "公开结果未通过保护校验")
                    if fault in {"parent_cancel", "service_close"}:
                        await release.wait()
                await original(value)

            monkeypatch.setattr(runtime, "validate_public_output", gate)
            task = asyncio.create_task(
                service.resume_thread(ThreadResumeParams(thread_id=thread.thread_id))
            )
            if fault in {"parent_cancel", "service_close"}:
                await asyncio.wait_for(reached.wait(), 2)
                assert not service._tasks and not provider.requests
                if fault == "parent_cancel":
                    task.cancel()
                else:
                    await service.close()
                    release.set()
            if fault == "safe":
                await asyncio.wait_for(task, 2)
                assert len(service._tasks) == 1
                await asyncio.wait_for(asyncio.gather(*tuple(service._tasks.values())), 2)
                assert len(provider.requests) == 1
                await service.resume_thread(ThreadResumeParams(thread_id=thread.thread_id))
                assert len(provider.requests) == 1
            elif fault == "parent_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert await store.get_thread(thread.thread_id) == before
                assert not service._tasks and not provider.requests
            else:
                with pytest.raises(AgentServiceError) as caught:
                    await asyncio.wait_for(task, 2)
                assert (
                    caught.value.code
                    == {
                        "registered_snapshot": "public_output_secret_leak",
                        "output_timeout": "public_output_timeout",
                        "service_close": "server_closing",
                    }[fault]
                )
                assert await store.get_thread(thread.thread_id) == before
                assert not service._tasks and not provider.requests
            await service.close()


@pytest.mark.parametrize("phase", ["input", "output"])
@pytest.mark.parametrize("fault", ["closed", "limit", "timeout", "extension", "parent_cancel"])
async def test_query_guard_failures_and_parent_cancel_do_not_mutate_session(
    tmp_path, monkeypatch, phase, fault
):
    from harnessix.agent import publication
    from harnessix.secrets import publication as secret_publication

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            service = service_for(runtime)
            original_read = runtime.store.get_thread
            read = AsyncMock(wraps=original_read)
            monkeypatch.setattr(runtime.store, "get_thread", read)
            name = "validate_public_input" if phase == "input" else "validate_public_output"
            original = getattr(runtime, name)
            reached = asyncio.Event()

            async def gate(value):
                # 错误DTO仍使用原保护器；只在指定原参数或Thread候选上注入故障。
                candidate = "threadId" in value if phase == "input" else "thread" in value
                if not candidate:
                    return await original(value)
                reached.set()
                if fault == "parent_cancel":
                    await asyncio.Event().wait()
                if fault == "closed":
                    scope.close()
                with monkeypatch.context() as patch:
                    if fault == "limit":
                        patch.setattr(secret_publication, "MAX_SCAN_NODES", 0)
                    if fault == "timeout":
                        patch.setattr(publication, "PUBLIC_PROTECTION_TIMEOUT", 0)
                    if fault == "extension":

                        def broken(_value, *, checkpoint):
                            raise ValueError(CANARY)

                        patch.setattr(scope, "assert_public_json", broken)
                    await original(value)

            monkeypatch.setattr(runtime, name, gate)
            task = asyncio.create_task(
                service.get_thread(ThreadGetParams(thread_id=thread.thread_id))
            )
            if fault == "parent_cancel":
                await asyncio.wait_for(reached.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(AgentServiceError) as caught:
                    await task
                suffix = {
                    "closed": "secret_unavailable",
                    "limit": "limit",
                    "timeout": "timeout",
                    "extension": "protection_failed",
                }[fault]
                assert caught.value.code == f"public_{phase}_{suffix}"
                assert CANARY not in str(caught.value)
            assert read.await_count == (0 if phase == "input" else 1)
            assert await original_read(thread.thread_id) == thread
            assert not service._tasks and not runtime.provider.requests
            await service.close()


@pytest.mark.parametrize("reason", ["close", "cancel", "timeout"])
async def test_real_empty_long_poll_has_bounded_close_cancel_and_timeout(
    tmp_path, monkeypatch, reason
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            service = service_for(runtime)
            reached = asyncio.Event()
            original_snapshot = service._next_snapshot

            async def observed(params, *, include_deltas):
                reached.set()
                return await original_snapshot(params, include_deltas=include_deltas)

            monkeypatch.setattr(service, "_next_snapshot", observed)
            task = asyncio.create_task(
                service.next_events(
                    EventsNextParams(
                        thread_id=thread.thread_id,
                        after_cursor=thread.sequence,
                        wait_ms=20 if reason == "timeout" else 30000,
                    )
                )
            )
            # 实际快照读取开始意味着信号已注册；仍调用原SQLite读取，不用轮询sleep。
            await asyncio.wait_for(reached.wait(), 2)
            assert thread.thread_id in service._delta_events
            if reason == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if reason == "close":
                    await service.close()
                result = await asyncio.wait_for(task, 2)
                assert result.timed_out and not result.replay.events
                assert result.replay.scanned_through == thread.sequence
            assert await runtime.store.get_thread(thread.thread_id) == thread
            assert not service._tasks
            await service.close()


async def test_artifact_candidate_full_original_dto_is_checked_without_rewriting_reference(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from harnessix.protocol.contracts import ArtifactPageResult, PublicArtifactRef

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            artifacts = AsyncMock(session=runtime.store)
            reader = ScopedProtocolArtifactReader(runtime.store, artifacts, AsyncMock())
            reference = PublicArtifactRef(
                artifact_id=uuid4(),
                sha256="a" * 64,
                size_bytes=20,
                records=1,
                format="jsonl/v1",
                complete=True,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            candidate = ArtifactPageResult(artifact=reference, offset=0, text=CANARY)
            read = AsyncMock(return_value=candidate)
            monkeypatch.setattr(reader, "read", read)
            service = service_for(runtime, reader)
            with pytest.raises(AgentServiceError) as caught:
                await service.read_artifact(
                    ArtifactReadParams(
                        thread_id=thread.thread_id, artifact_id=reference.artifact_id
                    )
                )
            assert caught.value.code == "public_output_secret_leak" and read.await_count == 1
            assert candidate.artifact == reference and candidate.text == CANARY
            assert await runtime.store.get_thread(thread.thread_id) == thread
            await service.close()


@pytest.mark.parametrize("surface", ["get", "resume", "replay", "next", "artifact", "list"])
async def test_registered_uuid_identity_is_rejected_before_query_io_or_registration(
    tmp_path, monkeypatch, surface
):
    from harnessix.product_config.contracts import SecretReference
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope

    identity = uuid4()
    source = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "9", "KEY"),), environment={"KEY": str(identity)}
    )
    with SecretPublicationScope((SecretReference(name="api", version="9"),), source) as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            service = service_for(runtime)
            read = AsyncMock(side_effect=AssertionError("原身份准入拒绝后不允许读取"))
            monkeypatch.setattr(runtime.store, "get_thread", read)
            monkeypatch.setattr(runtime.store, "list_thread_page", read)
            monkeypatch.setattr(runtime.store, "events", read)
            with pytest.raises(AgentServiceError) as caught:
                if surface == "list":
                    await service.list_threads(ThreadListParams(cursor=str(identity)))
                elif surface == "artifact":
                    await service.read_artifact(
                        ArtifactReadParams(thread_id=identity, artifact_id=uuid4())
                    )
                else:
                    await query(service, surface, identity)
            assert caught.value.code == "public_input_secret_leak" and not read.await_count
            assert not service._delta_events and not service._tasks
            await service.close()


async def test_current_scope_cannot_authorize_unregistered_old_direct_query_history(tmp_path):
    from harnessix.product_config.contracts import SecretReference
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([answer(CANARY)])) as legacy:
        thread = await legacy.create_thread(str(tmp_path))
        await legacy.run_turn(thread.thread_id, "safe", request_id="legacy")
    source = EnvironmentSecretProvider(
        (EnvironmentSecretSource("api", "10", "KEY"),),
        environment={"KEY": "different-fixture-model-material/+10"},
    )
    with SecretPublicationScope((SecretReference(name="api", version="10"),), source) as scope:
        async with AgentRuntime(
            store, ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            service = service_for(runtime)
            before = await store.get_thread(thread.thread_id)
            result = await service.replay_events(EventsReplayParams(thread_id=thread.thread_id))
            # 该观察通过表示开放风险仍存在，不是未知旧历史的授权验收通过。
            assert CANARY in result.model_dump_json()
            assert await store.get_thread(thread.thread_id) == before
            await service.close()


async def test_constructed_invalid_query_input_is_fixed_without_warning_or_io(
    tmp_path, monkeypatch, capsys
):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            service = service_for(runtime)
            read = AsyncMock()
            monkeypatch.setattr(runtime.store, "get_thread", read)
            params = ThreadGetParams.model_construct(thread_id=CANARY)
            with pytest.raises(AgentServiceError) as caught:
                await service.get_thread(params)
            assert caught.value.code == "invalid_params" and not read.await_count
            assert CANARY not in str(caught.value)
            assert not capsys.readouterr().err
            await service.close()


async def test_constructed_invalid_result_does_not_export_serializer_diagnostic(tmp_path, capsys):
    from harnessix.app_server.query_runtime import UnexpectedQueryError, execute_query
    from harnessix.protocol.contracts import ThreadResult

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            invalid = ThreadResult.model_construct(thread={"secret": CANARY})
            with pytest.raises(UnexpectedQueryError) as caught:
                await execute_query(
                    runtime,
                    ThreadGetParams(thread_id=thread.thread_id),
                    AsyncMock(return_value=invalid),
                )
            assert caught.value.code == "internal_error" and CANARY not in str(caught.value)
            assert not capsys.readouterr().err
            assert await runtime.store.get_thread(thread.thread_id) == thread


async def test_non_boolean_live_delta_option_is_rejected_before_signal_or_store(
    tmp_path, monkeypatch
):
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([])) as runtime:
        service = service_for(runtime)
        read = AsyncMock()
        monkeypatch.setattr(runtime.store, "events", read)
        with pytest.raises(AgentServiceError) as caught:
            await service.next_events(EventsNextParams(thread_id=uuid4()), include_deltas=CANARY)
        assert caught.value.code == "invalid_params" and not read.await_count
        assert not service._delta_events
        await service.close()
