from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.context import (
    ContextBuildInput,
    ContextEngine,
    ContextFragment,
    ContextFragmentKind,
    ContextInspectionV2,
    ContextLimits,
    ContextSourceError,
    ProjectInstructionSource,
    SourcedContextEngine,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import files
from harnessix.tools.contracts import ReadToolError
from tests.agent.helpers import RecordingTools, answer, tool_step


def request(root: Path) -> ContextBuildInput:
    return ContextBuildInput(
        thread_id=uuid4(),
        turn_id=uuid4(),
        model_step=1,
        workspace=str(root),
    )


def sourced(root: Path, *, working_directory: str = ".") -> SourcedContextEngine:
    return SourcedContextEngine(
        ContextEngine(
            ContextLimits(
                context_window_tokens=32_768,
                reserved_output_tokens=1024,
                provider_overhead_tokens=0,
                safety_margin_tokens=0,
            ),
            (
                ContextFragment(
                    kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
                    source="runtime",
                    content="运行时边界",
                ),
            ),
        ),
        (ProjectInstructionSource(root, working_directory=working_directory),),
    )


async def test_project_instructions_follow_root_to_cwd_and_override_precedence(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "service").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("根规则")
    (tmp_path / "src" / "AGENTS.md").write_text("被覆盖的规则")
    (tmp_path / "src" / "AGENTS.override.md").write_text("目录覆盖规则")
    (tmp_path / "src" / "service" / "AGENTS.md").write_text("服务规则")

    result = await sourced(tmp_path, working_directory="src/service").prepare(
        request(tmp_path), CancelToken()
    )

    assert isinstance(result.inspection, ContextInspectionV2)
    rendered = json.loads(result.instructions or "")
    project = [
        fragment
        for fragment in rendered["fragments"]
        if fragment["kind"] == ContextFragmentKind.PROJECT_INSTRUCTION
    ]
    assert [fragment["source"] for fragment in project] == [
        "AGENTS.md",
        "src/AGENTS.override.md",
        "src/service/AGENTS.md",
    ]
    assert [fragment["content"] for fragment in project] == [
        "根规则",
        "目录覆盖规则",
        "服务规则",
    ]
    snapshot = result.inspection.sources[0]
    assert snapshot.status == "available"
    assert [document.source for document in snapshot.documents] == [
        "AGENTS.md",
        "src/AGENTS.override.md",
        "src/service/AGENTS.md",
    ]
    assert "根规则" not in snapshot.model_dump_json()


async def test_missing_or_blank_instruction_is_an_explicit_empty_snapshot(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(" \n")
    result = await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert isinstance(result.inspection, ContextInspectionV2)
    snapshot = result.inspection.sources[0]
    assert snapshot.status == "empty"
    assert len(snapshot.documents) == 1
    assert snapshot.documents[0].utf8_bytes == 2
    assert snapshot.documents[0].fragment_id is None
    assert " \n" not in snapshot.model_dump_json()

    (tmp_path / "AGENTS.md").unlink()
    missing = await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert isinstance(missing.inspection, ContextInspectionV2)
    assert missing.inspection.sources[0].status == "empty"
    assert not missing.inspection.sources[0].documents


async def test_workspace_alias_must_resolve_to_the_same_bound_root(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("规则")
    alias = tmp_path / "root-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    aliased = request(tmp_path).model_copy(update={"workspace": str(alias)})
    result = await sourced(tmp_path).prepare(aliased, CancelToken())
    assert isinstance(result.inspection, ContextInspectionV2)
    assert result.inspection.sources[0].status == "available"


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink"])
async def test_unsafe_instruction_file_fails_closed(tmp_path: Path, unsafe: str) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("不得读取")
    target = tmp_path / "AGENTS.md"
    if unsafe == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)

    with pytest.raises(ContextSourceError) as captured:
        await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_invalid"
    assert not captured.value.retryable


async def test_instruction_limit_and_workspace_mismatch_fail_before_planning(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * 1024)
    limited = SourcedContextEngine(
        ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024)),
        (ProjectInstructionSource(tmp_path, max_total_bytes=100),),
    )
    with pytest.raises(ContextSourceError) as too_large:
        await limited.prepare(request(tmp_path), CancelToken())
    assert too_large.value.code == "context_source_too_large"

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ContextSourceError) as mismatch:
        await sourced(tmp_path).prepare(request(other), CancelToken())
    assert mismatch.value.code == "context_source_workspace_mismatch"


async def test_source_cancellation_stops_and_joins_workspace_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("规则")
    started = Event()
    finished = Event()
    original = files.read_file

    def blocked(*args, **kwargs):
        operation = args[2]
        started.set()
        try:
            while not operation.stopped.is_set():
                time.sleep(0.005)
            operation.checkpoint()
        finally:
            finished.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(files, "read_file", blocked)
    token = CancelToken()
    task = asyncio.create_task(sourced(tmp_path).prepare(request(tmp_path), token))
    assert await asyncio.to_thread(started.wait, 1)
    token.cancel()
    with pytest.raises(TurnCancelled):
        await task
    assert finished.is_set()


async def test_source_timeout_is_sanitized_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("规则")

    def timeout(*args, **kwargs):
        raise ReadToolError("timeout")

    monkeypatch.setattr(files, "read_file", timeout)
    with pytest.raises(ContextSourceError) as captured:
        await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_unavailable"
    assert captured.value.retryable
    assert "AGENTS.md" not in captured.value.message

    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "timeout.db")
    async with AgentRuntime(store, provider, async_context=sourced(tmp_path)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="source-timeout")
    assert turn.error is not None
    assert turn.error.code == "context_source_unavailable"
    assert turn.error.retryable and turn.error.category is FailureCategory.INPUT
    assert not provider.requests and not turn.context_inspections


@pytest.mark.parametrize("race", ["directory", "paged_file"])
async def test_source_detects_directory_and_paged_file_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    instruction = tmp_path / "AGENTS.md"
    instruction.write_text("line\n" * 7000 if race == "paged_file" else "规则")
    original = files.read_file
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        page = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            if race == "directory":
                (tmp_path / "created-during-observation.txt").write_text("变化")
            else:
                instruction.write_text("LINE\n" + "line\n" * 6999)
        return page

    monkeypatch.setattr(files, "read_file", changed)
    with pytest.raises(ContextSourceError) as captured:
        await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_unavailable"
    assert captured.value.retryable


class UpdatingTools(RecordingTools):
    def __init__(self, instruction: Path) -> None:
        super().__init__()
        self.instruction = instruction

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        result = await super().execute(call, cancel)
        self.instruction.write_text("第二版项目规则")
        return result


async def test_runtime_refreshes_and_persists_source_freshness_each_model_step(
    tmp_path: Path,
) -> None:
    instruction = tmp_path / "AGENTS.md"
    instruction.write_text("第一版项目规则")
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store,
        provider,
        UpdatingTools(instruction),
        async_context=sourced(tmp_path),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "刷新规则", request_id="source-refresh")
        inspected = await runtime.inspect_context(thread.thread_id, turn.turn_id)

    assert turn.status is TurnStatus.COMPLETED
    assert len(provider.requests) == len(turn.context_inspections) == 2
    assert "第一版项目规则" in (provider.requests[0].instructions or "")
    assert "第二版项目规则" in (provider.requests[1].instructions or "")
    first, second = turn.context_inspections
    assert isinstance(first, ContextInspectionV2) and isinstance(second, ContextInspectionV2)
    assert first.sources[0].source_revision != second.sources[0].source_revision
    assert first.sources[0].documents[0].revision != second.sources[0].documents[0].revision
    assert inspected == second
    assert "第一版项目规则" not in first.model_dump_json()
    events = await store.events(thread.thread_id)
    context_events = [event for event in events if event.payload.type == "context_prepared"]
    assert len(context_events) == 2
    assert all(event.schema_version == 11 for event in context_events)


async def test_source_failure_stops_before_provider_and_is_input_failure(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-runtime"
    outside.write_text("规则")
    (tmp_path / "AGENTS.md").symlink_to(outside)
    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, async_context=sourced(tmp_path)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="source-failure")
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None
    assert turn.error.code == "context_source_invalid"
    assert turn.error.category is FailureCategory.INPUT
    assert not provider.requests and not turn.context_inspections


async def test_source_trust_and_runtime_entry_are_fail_closed(tmp_path: Path) -> None:
    class PrivilegedSource:
        source_id = "runtime/illegal"
        kind = ContextFragmentKind.RUNTIME_INSTRUCTION

    with pytest.raises(ValueError, match="Runtime"):
        SourcedContextEngine(
            ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024)),
            (PrivilegedSource(),),  # type: ignore[arg-type]
        )

    with pytest.raises(KernelError) as conflict:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "conflict.db"),
            FakeProvider(),
            context=ContextEngine(
                ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024)
            ),
            async_context=sourced(tmp_path),
        )
    assert conflict.value.code == "context_runtime_conflict"


async def test_source_snapshot_must_match_fragment_kind_path_and_size(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("规则")
    result = await sourced(tmp_path).prepare(request(tmp_path), CancelToken())
    assert isinstance(result.inspection, ContextInspectionV2)
    encoded = result.inspection.model_dump(mode="json")
    encoded["sources"][0]["kind"] = ContextFragmentKind.ENVIRONMENT
    with pytest.raises(ValidationError, match="快照与 Fragment"):
        ContextInspectionV2.model_validate(encoded)
