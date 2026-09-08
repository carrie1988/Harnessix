from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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
    ContextConsistencySnapshot,
    ContextEngine,
    ContextFragment,
    ContextFragmentKind,
    ContextInspectionV2,
    ContextInspectionV3,
    ContextLimits,
    ContextSourceDocument,
    ContextSourceError,
    ContextSourceObservation,
    ContextSourceSnapshot,
    EnvironmentContextSource,
    GitContextSource,
    ProjectInstructionSource,
    SourcedContextEngine,
    WorkspaceContextSource,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import files
from harnessix.tools.contracts import ReadToolError
from tests.agent.helpers import RecordingTools, answer, tool_step


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git不可用")
    return Path(executable).resolve()


def git_command(root: Path, *arguments: str) -> None:
    subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def git_repository(root: Path) -> None:
    git_command(root, "init", "-q")
    git_command(root, "config", "user.name", "Harnessix Test")
    git_command(root, "config", "user.email", "test@harnessix.invalid")
    (root / "tracked.py").write_text("before\n")
    git_command(root, "add", "tracked.py")
    git_command(root, "commit", "-qm", "baseline")


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
    assert all(event.schema_version == 17 for event in context_events)


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


def multi_source_engine(root: Path, *sources: object) -> SourcedContextEngine:
    return SourcedContextEngine(
        ContextEngine(
            ContextLimits(
                context_window_tokens=65_536,
                reserved_output_tokens=1024,
                provider_overhead_tokens=0,
                safety_margin_tokens=0,
            )
        ),
        sources,  # type: ignore[arg-type]
    )


async def test_workspace_git_and_environment_sources_form_bounded_v3_context(
    tmp_path: Path,
) -> None:
    git_repository(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('changed')\n")
    (tmp_path / ".env").write_text("SECRET=not-visible\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "credential.txt").write_text("not-visible\n")
    (tmp_path / "tracked.py").write_text("after\n")
    environment = {
        "CI": "true",
        "NODE_ENV": "test",
        "OPENAI_API_KEY": "must-not-be-read",
    }
    engine = multi_source_engine(
        tmp_path,
        WorkspaceContextSource(tmp_path, working_directory="src", denied_paths=("private",)),
        GitContextSource(
            tmp_path,
            git_executable(),
            working_directory="src",
            denied_paths=("private",),
        ),
        EnvironmentContextSource(
            tmp_path,
            values=environment,
            allowlist=("CI", "NODE_ENV"),
            working_directory="src",
            denied_paths=("private",),
        ),
    )

    result = await engine.prepare(request(tmp_path), CancelToken())

    assert isinstance(result.inspection, ContextInspectionV3)
    assert result.inspection.consistency == ContextConsistencySnapshot(
        source_count=3,
        workspace_scope=result.inspection.sources[0].workspace_scope,
    )
    rendered = json.loads(result.instructions or "")
    contents = {
        fragment["kind"]: json.loads(fragment["content"]) for fragment in rendered["fragments"]
    }
    workspace = contents[ContextFragmentKind.WORKSPACE]
    root_names = {entry["name"] for entry in workspace["directories"][0]["entries"]}
    assert "src" in root_names
    assert not {".env", ".git", "private"} & root_names
    assert workspace["working_directory"] == "src"
    assert contents[ContextFragmentKind.GIT]["repository"] is True
    assert contents[ContextFragmentKind.GIT]["total_entries"] == 4
    git_paths = {entry["path"] for entry in contents[ContextFragmentKind.GIT]["entries"]}
    assert not {".env", "private/credential.txt"} & git_paths
    assert contents[ContextFragmentKind.ENVIRONMENT]["variables"] == {
        "CI": "true",
        "NODE_ENV": "test",
    }
    persisted = result.inspection.model_dump_json()
    assert "must-not-be-read" not in (result.instructions or "")
    assert "print('changed')" not in (result.instructions or "")
    assert "true" not in persisted and "test" not in persisted


async def test_workspace_source_truncates_view_and_detects_directory_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(30):
        (tmp_path / f"{index:02d}-{'x' * 40}.txt").write_text("content")
    source = WorkspaceContextSource(tmp_path, max_content_bytes=512)
    result = await SourcedContextEngine(
        ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=512)),
        (source,),
    ).prepare(request(tmp_path), CancelToken())
    payload = json.loads(
        next(
            fragment["content"]
            for fragment in json.loads(result.instructions or "")["fragments"]
            if fragment["kind"] == ContextFragmentKind.WORKSPACE
        )
    )
    assert payload["directories"][0]["truncated"] is True
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 512

    original = files.list_files
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        page = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            (tmp_path / "created-during-observation.txt").write_text("changed")
        return page

    monkeypatch.setattr(files, "list_files", changed)
    with pytest.raises(ContextSourceError) as captured:
        await source.observe(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_unavailable" and captured.value.retryable


async def test_git_source_has_explicit_non_repository_and_bounded_dirty_semantics(
    tmp_path: Path,
) -> None:
    non_repository = GitContextSource(tmp_path, git_executable())
    missing = await non_repository.observe(request(tmp_path), CancelToken())
    assert json.loads(missing.documents[0].content) == {
        "schema": "harnessix.git-context/v1",
        "repository": False,
    }

    git_repository(tmp_path)
    for index in range(30):
        (tmp_path / f"untracked-{index:02d}-{'y' * 80}.txt").write_text("new\n")
    bounded = GitContextSource(tmp_path, git_executable(), max_content_bytes=1024)
    observed = await bounded.observe(request(tmp_path), CancelToken())
    content = json.loads(observed.documents[0].content)
    assert content["repository"] is True
    assert content["total_entries"] == 30
    assert content["truncated"] is True
    assert len(observed.documents[0].content.encode()) <= 1024
    assert len(content["entries"]) < content["total_entries"]


async def test_git_source_timeout_is_sanitized_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GitContextSource(tmp_path, git_executable())

    async def timeout(*args, **kwargs):
        raise ReadToolError("timeout")

    monkeypatch.setattr(source._runtime, "execute", timeout)
    with pytest.raises(ContextSourceError) as captured:
        await source.observe(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_unavailable"
    assert captured.value.retryable and str(tmp_path) not in captured.value.message


async def test_git_source_filters_denied_rename_origin(tmp_path: Path) -> None:
    git_repository(tmp_path)
    (tmp_path / ".env").write_text("SECRET=tracked-but-not-visible\n")
    git_command(tmp_path, "add", "-f", ".env")
    git_command(tmp_path, "commit", "-qm", "track denied fixture")
    git_command(tmp_path, "mv", ".env", "visible.txt")

    observed = await GitContextSource(tmp_path, git_executable()).observe(
        request(tmp_path), CancelToken()
    )
    content = json.loads(observed.documents[0].content)

    assert content["total_entries"] == 1
    assert content["entries"] == []
    assert content["truncated"] is True
    assert "tracked-but-not-visible" not in observed.documents[0].content


async def test_environment_source_never_enumerates_mapping_and_rejects_secrets(
    tmp_path: Path,
) -> None:
    class NonIterableMapping(dict[str, str]):
        def __iter__(self):
            raise AssertionError("环境Source不得枚举宿主映射")

    values = NonIterableMapping(CI="true", OPENAI_API_KEY="not-visible")
    source = EnvironmentContextSource(tmp_path, values=values, allowlist=("CI",))
    observed = await source.observe(request(tmp_path), CancelToken())
    content = json.loads(observed.documents[0].content)
    assert content["variables"] == {"CI": "true"}
    assert "not-visible" not in observed.documents[0].content

    for secret in ("OPENAI_API_KEY", "AUTH_TOKEN", "PRIVATE_KEY", "SESSION_ID"):
        with pytest.raises(ValueError, match="Secret"):
            EnvironmentContextSource(tmp_path, values=values, allowlist=(secret,))

    with pytest.raises(ValueError, match="映射"):
        EnvironmentContextSource(
            tmp_path,
            values="CI=true",  # type: ignore[arg-type]
            allowlist=("CI",),
        )

    class FailingMapping(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            raise RuntimeError("不得传播的宿主异常")

    failing = EnvironmentContextSource(tmp_path, values=FailingMapping(), allowlist=("CI",))
    with pytest.raises(ContextSourceError) as mapping_error:
        await failing.observe(request(tmp_path), CancelToken())
    assert mapping_error.value.code == "context_source_invalid"
    assert "宿主异常" not in mapping_error.value.message

    oversized = EnvironmentContextSource(tmp_path, values={"LONG": "x" * 1025}, allowlist=("LONG",))
    with pytest.raises(ContextSourceError) as captured:
        await oversized.observe(request(tmp_path), CancelToken())
    assert captured.value.code == "context_source_too_large"

    for invalid in (123, "line\nbreak"):
        invalid_source = EnvironmentContextSource(
            tmp_path,
            values={"BUILD_LABEL": invalid},  # type: ignore[dict-item]
            allowlist=("BUILD_LABEL",),
        )
        with pytest.raises(ContextSourceError) as invalid_error:
            await invalid_source.observe(request(tmp_path), CancelToken())
        assert invalid_error.value.code == "context_source_invalid"


async def test_git_source_represents_initial_and_detached_head(tmp_path: Path) -> None:
    git_command(tmp_path, "init", "-q")
    initial = await GitContextSource(tmp_path, git_executable()).observe(
        request(tmp_path), CancelToken()
    )
    initial_content = json.loads(initial.documents[0].content)
    assert initial_content["repository"] is True and initial_content["head_oid"] is None

    git_command(tmp_path, "config", "user.name", "Harnessix Test")
    git_command(tmp_path, "config", "user.email", "test@harnessix.invalid")
    (tmp_path / "tracked.py").write_text("content\n")
    git_command(tmp_path, "add", "tracked.py")
    git_command(tmp_path, "commit", "-qm", "baseline")
    git_command(tmp_path, "checkout", "-q", "--detach", "HEAD")
    detached = await GitContextSource(tmp_path, git_executable()).observe(
        request(tmp_path), CancelToken()
    )
    detached_content = json.loads(detached.documents[0].content)
    assert detached_content["branch"] is None
    assert len(detached_content["head_oid"]) == 40


class SequencedSource:
    kind = ContextFragmentKind.ENVIRONMENT

    def __init__(self, source_id: str, observations: tuple[ContextSourceObservation, ...]) -> None:
        self.source_id = source_id
        self.observations = observations
        self.calls = 0

    async def observe(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> ContextSourceObservation:
        cancel.checkpoint()
        selected = self.observations[min(self.calls, len(self.observations) - 1)]
        self.calls += 1
        return selected


def observation(
    *, scope: str = "a", revision: str = "b", content: str = ""
) -> ContextSourceObservation:
    documents = ()
    if content:
        documents = (
            ContextSourceDocument(
                source="environment/test.json",
                content=content,
                revision="c" * 64,
            ),
        )
    return ContextSourceObservation(
        workspace_scope=scope * 64,
        source_revision=revision * 64,
        documents=documents,
    )


@pytest.mark.parametrize(
    ("first", "second", "expected", "retryable"),
    [
        (observation(revision="b"), observation(revision="d"), "context_sources_changed", True),
        (
            observation(content="first"),
            observation(content="second"),
            "context_source_invalid",
            False,
        ),
    ],
)
async def test_multi_source_double_observation_fails_closed_on_drift(
    tmp_path: Path,
    first: ContextSourceObservation,
    second: ContextSourceObservation,
    expected: str,
    retryable: bool,
) -> None:
    changing = SequencedSource("environment/changing", (first, second))
    stable = SequencedSource("environment/stable", (observation(),))
    with pytest.raises(ContextSourceError) as captured:
        await multi_source_engine(tmp_path, changing, stable).prepare(
            request(tmp_path), CancelToken()
        )
    assert captured.value.code == expected and captured.value.retryable is retryable


async def test_multi_source_detects_common_workspace_scope_change(tmp_path: Path) -> None:
    sources = tuple(
        SequencedSource(
            f"environment/scope-{index}",
            (observation(scope="a"), observation(scope="d")),
        )
        for index in range(2)
    )
    with pytest.raises(ContextSourceError) as captured:
        await multi_source_engine(tmp_path, *sources).prepare(request(tmp_path), CancelToken())
    assert captured.value.code == "context_sources_changed"
    assert captured.value.retryable is True


async def test_multi_source_rejects_mismatched_workspace_scope_before_provider(
    tmp_path: Path,
) -> None:
    first = SequencedSource("environment/first", (observation(scope="a"),))
    second = SequencedSource("environment/second", (observation(scope="d"),))
    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "mismatch.db")
    async_context = multi_source_engine(tmp_path, first, second)
    async with AgentRuntime(store, provider, async_context=async_context) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="scope-mismatch")
    assert turn.error is not None
    assert turn.error.code == "context_source_workspace_mismatch"
    assert turn.error.category is FailureCategory.INPUT
    assert not provider.requests and not turn.context_inspections


def test_pure_engine_cannot_bypass_multi_source_consistency() -> None:
    snapshots = tuple(
        ContextSourceSnapshot(
            source_id=f"environment/source-{index}",
            kind=ContextFragmentKind.ENVIRONMENT,
            status="empty",
            workspace_scope="a" * 64,
            source_revision=str(index) * 64,
        )
        for index in (1, 2)
    )
    with pytest.raises(ValueError, match="一致性"):
        ContextEngine(
            ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024)
        ).prepare_sourced(
            ContextBuildInput(thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace="/tmp"),
            (),
            snapshots,
        )


async def test_multi_source_v3_is_persisted_and_replayed_as_event_v12(tmp_path: Path) -> None:
    async_context = multi_source_engine(
        tmp_path,
        WorkspaceContextSource(tmp_path),
        EnvironmentContextSource(tmp_path, values={"CI": "true"}, allowlist=("CI",)),
    )
    store = SQLiteSessionStore(tmp_path / "v12.db")
    async with AgentRuntime(
        store, ScriptedProvider([answer()]), async_context=async_context
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="v12")
    assert turn.status is TurnStatus.COMPLETED
    assert len(turn.context_inspections) == 1
    assert isinstance(turn.context_inspections[0], ContextInspectionV3)
    events = await store.events(thread.thread_id)
    assert all(event.schema_version == 17 for event in events)
    assert await store.rebuild(thread.thread_id) == await store.get_thread(thread.thread_id)


class UpdatingEnvironmentTools(RecordingTools):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self.values = values

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        result = await super().execute(call, cancel)
        self.values["BUILD_LABEL"] = "second"
        return result


async def test_multi_source_refreshes_allowlisted_environment_each_model_step(
    tmp_path: Path,
) -> None:
    values = {"BUILD_LABEL": "first"}
    async_context = multi_source_engine(
        tmp_path,
        WorkspaceContextSource(tmp_path),
        EnvironmentContextSource(tmp_path, values=values, allowlist=("BUILD_LABEL",)),
    )
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "refresh-v12.db")
    async with AgentRuntime(
        store,
        provider,
        UpdatingEnvironmentTools(values),
        async_context=async_context,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "刷新环境", request_id="env-refresh")

    assert turn.status is TurnStatus.COMPLETED
    assert "first" in (provider.requests[0].instructions or "")
    assert "second" in (provider.requests[1].instructions or "")
    first, second = turn.context_inspections
    assert isinstance(first, ContextInspectionV3) and isinstance(second, ContextInspectionV3)
    assert first.sources[1].source_revision != second.sources[1].source_revision
