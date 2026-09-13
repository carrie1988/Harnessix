from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent import runtime as runtime_module
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ModelHistoryPrepared, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactRef, ArtifactToolResult
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.context import ContextEngine, ContextLimits
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import history_document
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import exercise, results, step
from tests.helpers import RecordingObservability


class ReadingProvider(ScriptedProvider):
    async def stream(self, request, cancel):
        if request.step == 2:
            result = next(
                i.content for i in request.history if isinstance(i.content, ToolResultContent)
            )
            self.steps = (
                self.steps[0],
                tuple(
                    step(
                        "read_artifact",
                        artifact_id=result.output["artifact"]["artifact_id"],
                        offset=250,
                        limit=2,
                    )
                ),
                self.steps[2],
            )
        async for event in super().stream(request, cancel):
            yield event


class RecordingContext:
    def __init__(self):
        self.requests = []
        self.engine = ContextEngine(
            ContextLimits(context_window_tokens=32768, reserved_output_tokens=1024)
        )

    def prepare(self, request):
        self.requests.append(request)
        return self.engine.prepare(request)


async def test_real_search_reduction_page_replay_context_and_both_provider_mappings(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文🙂\n" * 300)
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store)
    provider = ReadingProvider([step(query="needle", max_results=40), answer(), answer()])
    context = RecordingContext()
    observer = RecordingObservability()
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            context=context,
            observability=observer,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "查找并回读", request_id="search")
        assert turn.status == TurnStatus.COMPLETED, turn.error
        original, page = results(turn)
        assert len(original.output["preview"]["matches"]) == 40
        assert page.output["offset"] == 250
        view = next(
            i.content
            for i in provider.requests[1].history
            if isinstance(i.content, ToolResultContent)
        )
        assert view.output["preview"] == {
            k: v for k, v in original.output["preview"].items() if k != "matches"
        }
        assert view.output["model_view"]["omitted_field"] == "matches"
        assert view.output["artifact"] == original.output["artifact"]
        assert [d.strategy for d in turn.tool_result_view_decisions] == [
            "artifact_reference",
            "inline",
        ]
        assert turn.tool_result_view_decisions[1].references[0].purpose == "artifact_page"
        assert [i.model_step for i in turn.model_history_inspections] == [1, 2, 3]
        for request, context_request in zip(provider.requests, context.requests, strict=True):
            assert context_request.history_documents == tuple(
                history_document(i) for i in request.history
            )
        # Provider只映射已准备副本；UTF-8预算不依赖供应商JSON键顺序。
        chat, _ = chat_request(provider.requests[-1], OpenAIChatConfig(model="fixture"))
        anthropic, _ = anthropic_request(provider.requests[-1], AnthropicConfig(model="fixture"))
        chat_results = [m["content"] for m in chat["messages"] if m["role"] == "tool"]
        anthropic_results = [
            b["content"]
            for m in anthropic["messages"]
            for b in m["content"]
            if b["type"] == "tool_result"
        ]
        assert chat_results == anthropic_results
        assert [len(value.encode()) for value in chat_results] == [
            d.view_utf8_bytes for d in turn.tool_result_view_decisions
        ]
        assert json.loads(chat_results[0])["output"] == view.output
    events = await store.events(thread.thread_id)
    committed = [e for e in events if isinstance(e.payload, ModelHistoryPrepared)]
    assert [len(e.payload.decisions) for e in committed] == [0, 1, 1]
    assert all(e.schema_version == 20 for e in committed)
    stored = await store.get_thread(thread.thread_id)
    assert replay(events) == stored == await store.rebuild(thread.thread_id)
    reopened = SQLiteSessionStore(store.path)
    await reopened.initialize()
    assert await reopened.get_thread(thread.thread_id) == stored
    metrics = [m for m in observer.metrics if "model_history" in m[1]]
    assert metrics and any(
        m[2] == 1 and m[3] == {"strategy": "artifact_reference"} for m in metrics
    )
    labels = json.dumps([m[3] for m in metrics])
    assert str(thread.thread_id) not in labels and "needle" not in labels and "sha256" not in labels


@pytest.mark.parametrize(
    "kind", ["thread", "call", "scope", "purpose", "manifest", "body", "expired", "missing"]
)
async def test_artifact_verifier_checks_every_binding_and_body(tmp_path, monkeypatch, kind):
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    ref = ArtifactRef.model_validate_json(json.dumps(result.output["artifact"]))
    args = [thread.thread_id, result.call_id, ref]
    kwargs = {"workspace_scope": scope, "purpose": "tool_result"}
    await artifacts.verify_reference(*args, **kwargs)
    if kind == "thread":
        args[0] = uuid4()
    elif kind == "call":
        args[1] = uuid4()
    elif kind == "scope":
        kwargs["workspace_scope"] = "0" * 64
    elif kind == "purpose":
        kwargs["purpose"] = "batch_effect"
    elif kind == "manifest":
        args[2] = ref.model_copy(update={"sha256": "0" * 64})
    elif kind == "body":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE agent_artifacts SET body = zeroblob(size_bytes)")
    elif kind == "expired":
        monkeypatch.setattr(
            artifact_sqlite, "utc_now", lambda: ref.expires_at + timedelta(seconds=1)
        )
    else:
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM agent_artifacts")
    with pytest.raises(KernelError) as error:
        await artifacts.verify_reference(*args, **kwargs)
    assert error.value.code == (
        "artifact_corrupt"
        if kind in {"manifest", "body"}
        else "artifact_expired"
        if kind == "expired"
        else "artifact_not_found"
    )


@pytest.mark.parametrize(
    "mode", ["missing_verifier", "missing_scope", "expired", "body", "scope", "root", "closed"]
)
async def test_invalid_history_stops_before_provider(tmp_path, monkeypatch, mode):
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=2)
    provider = FakeProvider()
    if mode == "expired":
        ref = ArtifactRef.model_validate_json(json.dumps(results(turn)[0].output["artifact"]))
        monkeypatch.setattr(
            artifact_sqlite, "utc_now", lambda: ref.expires_at + timedelta(seconds=1)
        )
    elif mode == "body":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE agent_artifacts SET body = zeroblob(size_bytes)")
    root = tmp_path / "repo"
    async with CodingToolRuntime(
        root, artifacts=artifacts, denied_paths=("main.py",) if mode == "scope" else ()
    ) as tools:
        if mode == "root":
            root.rename(tmp_path / "old")
            root.mkdir()
        elif mode == "closed":
            await tools.aclose()
        async with AgentRuntime(
            store,
            provider,
            artifact_verifier=None if mode == "missing_verifier" else artifacts,
            artifact_access=None if mode == "missing_scope" else tools,
        ) as runtime:
            failed = await runtime.run_turn(thread.thread_id, "继续", request_id="invalid")
    assert failed.status == TurnStatus.FAILED and not provider.requests
    assert not failed.model_history_inspections
    assert (
        failed.error.code
        == {
            "missing_verifier": "context_artifact_verifier_required",
            "missing_scope": "context_artifact_scope_required",
            "expired": "artifact_expired",
            "body": "artifact_corrupt",
            "scope": "artifact_not_found",
            "root": "workspace_changed",
            "closed": "tool_runtime_closed",
        }[mode]
    )


@pytest.mark.parametrize("kind", ["user", "task", "timeout"])
async def test_verifier_cancellation_timeout_drains_without_provider_or_decision(
    tmp_path, monkeypatch, kind
):
    store, artifacts, _, thread, _ = await exercise(tmp_path, count=1)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def block(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(artifacts, "verify_reference", block)
    if kind == "timeout":
        monkeypatch.setattr(runtime_module, "HISTORY_ARTIFACT_TIMEOUT_SECONDS", 0.05)
    provider = FakeProvider()
    async with CodingToolRuntime(tmp_path / "repo", artifacts=artifacts) as tools:
        async with AgentRuntime(
            store, provider, artifact_verifier=artifacts, artifact_access=tools
        ) as runtime:
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消验证", request_id="cancel")
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                if kind == "task":
                    task.cancel()
                elif kind == "user":
                    active = (await store.get_thread(thread.thread_id)).turns[-1]
                    await runtime.cancel(thread.thread_id, active.turn_id)
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    final = (await store.get_thread(thread.thread_id)).turns[-1]
    assert cleaned.is_set() and not provider.requests and not final.model_history_inspections
    assert final.status == (TurnStatus.FAILED if kind == "timeout" else TurnStatus.CANCELLED)
    if kind == "timeout":
        assert final.error.code == "context_artifact_timeout" and final.error.retryable


@pytest.mark.parametrize("mode", ["not_covered", "incomplete"])
async def test_reduction_requires_actual_coverage_not_just_complete_manifest(tmp_path, mode):
    class WrongPreview(CodingToolRuntime):
        async def execute_scoped(self, call, scope, cancel):
            output = await super().execute_scoped(call, scope, cancel)
            assert isinstance(output, ArtifactToolResult)
            if mode == "not_covered":
                output = replace(
                    output,
                    result=output.result.model_copy(update={"output": {"unarchived": "x" * 10000}}),
                )
            else:
                output = replace(output, complete=False)
            return output

    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * 100)
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store)
    provider = ScriptedProvider([step(query="needle", max_results=50), answer()])
    async with WrongPreview(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "完整性检查", request_id="coverage")
    assert turn.status == TurnStatus.FAILED and len(provider.requests) == 1
    assert turn.error.code == (
        "artifact_corrupt" if mode == "not_covered" else "context_tool_result_artifact_incomplete"
    )
    assert not turn.tool_result_view_decisions and len(turn.model_history_inspections) == 1
    assert results(turn)[0].outcome == "succeeded"  # 工具执行事实不随视图失败被抹去。


@pytest.mark.parametrize(
    "point,committed",
    [
        ("runtime.after_history_artifacts_verified", False),
        ("runtime.after_model_history_prepared", True),
    ],
)
async def test_real_crash_reopen_does_not_repeat_tool_or_model(tmp_path, point, committed):
    import sys
    from pathlib import Path

    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * 100)
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(root.resolve()))
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.artifacts.model_history_crash_worker",
        str(store.path),
        str(thread.thread_id),
        str(root),
        point,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(child.wait(), 20) == 88
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    before = (await store.get_thread(thread.thread_id)).turns[-1]
    assert before.status == TurnStatus.PREPARING_CONTEXT
    assert len(before.model_history_inspections) == 1 + committed
    assert len(before.tool_result_view_decisions) == int(committed)
    assert len(results(before)[0].output["preview"]["matches"]) == 40
    provider = FakeProvider()
    async with AgentRuntime(store, provider) as runtime:
        interrupted = (await store.get_thread(thread.thread_id)).turns[-1]
        assert interrupted.status == TurnStatus.INTERRUPTED
        assert interrupted.tool_result_view_decisions == before.tool_result_view_decisions
        assert interrupted.model_history_inspections == before.model_history_inspections
        assert results(interrupted) == results(before)
        assert await runtime.resume_turn(thread.thread_id, interrupted.turn_id) == interrupted
        assert not provider.requests
    with sqlite3.connect(store.path) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_glob_omits_only_archived_paths_and_preserves_search_statistics(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(45):
        (root / (str(index).zfill(3) + "long" * 35 + ".py")).touch()
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store)
    provider = ScriptedProvider([step("glob", pattern="*.py", max_results=40), answer()])
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "定位文件", request_id="glob")
    assert turn.status == TurnStatus.COMPLETED, turn.error
    source = results(turn)[0].output["preview"]
    (decision,) = turn.tool_result_view_decisions
    assert decision.strategy == "artifact_reference"
    assert decision.replacement_output["preview"] == {
        key: value for key, value in source.items() if key != "paths"
    }
    assert decision.replacement_output["model_view"]["omitted_field"] == "paths"


def test_coverage_does_not_confuse_boolean_with_number():
    from tests.context.test_tool_result_view import sample

    thread = sample([True], archived=True)
    with pytest.raises(KernelError) as error:
        SQLiteArtifactStore._verify_coverage(
            thread, thread.turns[0].items[-1].content.call_id, ["1"], "preview"
        )
    assert error.value.code == "artifact_corrupt"
