from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools import git as git_tools
from harnessix.tools import runtime as tool_runtime
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime
from harnessix.tools.git_contracts import GitDiffOutput, GitReviewOutput, GitStatusOutput
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute


def _git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git不可用")
    return Path(executable).resolve()


def _command(root: Path, *arguments: str) -> None:
    subprocess.run(
        [str(_git()), *arguments],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def _repository(root: Path, *, object_format: str | None = None) -> None:
    arguments = (f"--object-format={object_format}",) if object_format is not None else ()
    _command(root, "init", "-q", *arguments)
    _command(root, "config", "user.name", "Harnessix Test")
    _command(root, "config", "user.email", "test@harnessix.invalid")
    (root / "tracked.py").write_text("before\n", encoding="utf-8")
    _command(root, "add", "tracked.py")
    _command(root, "commit", "-qm", "baseline")


async def test_git_tools_are_opt_in_and_bind_executable(tmp_path: Path) -> None:
    async with CodingToolRuntime(tmp_path) as tools:
        assert "git_status" not in {item.name for item in tools.definitions()}
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        definitions = {item.name: item for item in tools.definitions()}
        assert {"git_status", "git_diff", "git_review"} <= definitions.keys()
        assert all(
            definitions[name].effect_class.value == "read_only"
            and not definitions[name].requires_approval
            and definitions[name].supports_parallel_calls
            for name in ("git_status", "git_diff", "git_review")
        )


async def test_git_status_is_structured_bounded_and_detects_rename(tmp_path: Path) -> None:
    _repository(tmp_path)
    _command(tmp_path, "mv", "tracked.py", "renamed.py")
    _command(tmp_path, "add", "-A")
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        result = await execute(tools, "git_status", limit=1)
    assert result.outcome == "succeeded"
    output = result.output
    assert output["branch"] in {"main", "master"}
    assert len(output["head_oid"]) == 40
    assert output["total_entries"] == 2 and output["truncated"] is True
    assert output["entries"] == [
        {
            "path": "renamed.py",
            "original_path": "tracked.py",
            "kind": "renamed",
            "index_status": "R",
            "worktree_status": ".",
            "submodule": "N...",
        }
    ]
    assert len(output["revision"]) == 64


async def test_git_diff_worktree_and_staged_are_explicit(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "tracked.py").write_text("中间\n", encoding="utf-8")
    _command(tmp_path, "add", "tracked.py")
    (tmp_path / "tracked.py").write_text("最后\n", encoding="utf-8")
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        worktree = await execute(tools, "git_diff", target="worktree", context_lines=0)
        staged = await execute(tools, "git_diff", target="staged", context_lines=0)
    assert worktree.outcome == staged.outcome == "succeeded"
    assert "-中间" in worktree.output["text"] and "+最后" in worktree.output["text"]
    assert "-before" in staged.output["text"] and "+中间" in staged.output["text"]
    assert not worktree.output["truncated"] and not staged.output["truncated"]


async def test_git_diff_reports_utf8_prefix_and_full_digest(tmp_path: Path) -> None:
    _repository(tmp_path)
    body = ("中文差异" * 30_000) + "\n"
    (tmp_path / "tracked.py").write_text(body, encoding="utf-8")
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        result = await execute(tools, "git_diff")
    assert result.outcome == "succeeded"
    output = result.output
    assert output["truncated"] is True
    assert output["utf8_bytes"] <= 48 * 1024 < output["observed_bytes"]
    assert len(output["text"].encode()) == output["utf8_bytes"]
    assert len(output["observed_sha256"]) == 64


async def test_git_review_returns_versioned_status_and_worktree_diff(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "tracked.py").write_text("staged\n", encoding="utf-8")
    _command(tmp_path, "add", "tracked.py")
    (tmp_path / "tracked.py").write_text("worktree\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")

    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        result = await execute(tools, "git_review", limit=1, context_lines=0)

    assert result.outcome == "succeeded"
    output = GitReviewOutput.model_validate_json(json.dumps(result.output))
    assert output.spec_version == "harnessix.git-review/v1"
    assert output.observation_mode == "sequential_non_atomic"
    assert output.status.total_entries == 2
    assert len(output.status.entries) == 1 and output.status.truncated
    assert output.diff.target == "worktree"
    assert "-staged" in output.diff.text and "+worktree" in output.diff.text
    assert "-before" not in output.diff.text


@pytest.mark.parametrize("failed_operation", ["status", "diff"])
async def test_git_review_fails_without_partial_output_when_either_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_operation: str
) -> None:
    _repository(tmp_path)
    original_run = GitReadRuntime._run
    operations: list[str] = []

    async def fail_selected(
        runtime: GitReadRuntime,
        arguments: tuple[str, ...],
        cancel: CancelToken,
        *,
        repository_check: bool = False,
    ):
        operation = "status" if "status" in arguments else "diff" if "diff" in arguments else None
        if operation is not None:
            operations.append(operation)
            if operation == failed_operation:
                raise ReadToolError("io_failed")
        return await original_run(
            runtime, arguments, cancel, repository_check=repository_check
        )

    monkeypatch.setattr(GitReadRuntime, "_run", fail_selected)
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        result = await execute(tools, "git_review")

    assert result.outcome == "failed"
    assert result.error.code == "tool_io_failed"
    assert result.output is None
    assert operations == (["status"] if failed_operation == "status" else ["status", "diff"])


@pytest.mark.parametrize("invalid_operation", ["status", "diff"])
async def test_git_review_rejects_invalid_child_output_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_operation: str
) -> None:
    _repository(tmp_path)
    revision = "0" * 64
    if invalid_operation == "status":
        wrong_output = GitDiffOutput(
            target="worktree",
            text="",
            utf8_bytes=0,
            observed_bytes=0,
            observed_sha256=revision,
            truncated=False,
        )
        monkeypatch.setattr(git_tools, "_status", lambda *_: wrong_output)
    else:
        wrong_output = GitStatusOutput(
            branch=None,
            head_oid=None,
            upstream=None,
            ahead=None,
            behind=None,
            entries=(),
            total_entries=0,
            truncated=False,
            revision=revision,
        )
        monkeypatch.setattr(git_tools, "_diff", lambda *_: wrong_output)

    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        with pytest.raises(KernelError) as invalid:
            await execute(tools, "git_review")
    assert invalid.value.code == "tool_output_invalid"


async def test_git_review_enforces_combined_result_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    (tmp_path / "tracked.py").write_text("after\n", encoding="utf-8")
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        initial = await execute(tools, "git_review")
    assert initial.outcome == "succeeded"
    encoded_bytes = len(
        GitReviewOutput.model_validate_json(json.dumps(initial.output)).model_dump_json().encode()
    )

    monkeypatch.setattr(git_tools, "MAX_RESULT_BYTES", encoded_bytes)
    monkeypatch.setattr(tool_runtime, "MAX_RESULT_BYTES", encoded_bytes)
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        exact = await execute(tools, "git_review")
    assert exact.outcome == "succeeded"

    monkeypatch.setattr(git_tools, "MAX_RESULT_BYTES", encoded_bytes - 1)
    monkeypatch.setattr(tool_runtime, "MAX_RESULT_BYTES", encoded_bytes - 1)
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        exceeded = await execute(tools, "git_review")
    assert exceeded.outcome == "failed"
    assert exceeded.error.code == "tool_limit_exceeded"
    assert exceeded.output is None


@pytest.mark.parametrize("cancel_kind", ["token", "task"])
async def test_git_review_cancellation_after_status_never_returns_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_kind: str
) -> None:
    _repository(tmp_path)
    original_run = GitReadRuntime._run
    diff_started = asyncio.Event()
    release = asyncio.Event()
    status_completed = False

    async def block_diff(
        runtime: GitReadRuntime,
        arguments: tuple[str, ...],
        cancel: CancelToken,
        *,
        repository_check: bool = False,
    ):
        nonlocal status_completed
        if "status" in arguments:
            result = await original_run(
                runtime, arguments, cancel, repository_check=repository_check
            )
            status_completed = True
            return result
        if "diff" in arguments:
            diff_started.set()
            await release.wait()
        return await original_run(
            runtime, arguments, cancel, repository_check=repository_check
        )

    monkeypatch.setattr(GitReadRuntime, "_run", block_diff)
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        token = CancelToken()
        task = asyncio.create_task(tools.execute(call(tools, "git_review"), token))
        try:
            await asyncio.wait_for(diff_started.wait(), 10)
            assert status_completed
            token.cancel() if cancel_kind == "token" else task.cancel()
            with pytest.raises(TurnCancelled if cancel_kind == "token" else asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_git_review_close_waits_for_whole_active_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path)
    original_run = GitReadRuntime._run
    diff_started = asyncio.Event()
    release = asyncio.Event()

    async def block_diff(
        runtime: GitReadRuntime,
        arguments: tuple[str, ...],
        cancel: CancelToken,
        *,
        repository_check: bool = False,
    ):
        if "diff" in arguments:
            diff_started.set()
            await release.wait()
        return await original_run(
            runtime, arguments, cancel, repository_check=repository_check
        )

    monkeypatch.setattr(GitReadRuntime, "_run", block_diff)
    tools = CodingToolRuntime(tmp_path, git_executable=_git())
    review_call = call(tools, "git_review")
    review = asyncio.create_task(tools.execute(review_call, CancelToken()))
    closing = None
    try:
        await asyncio.wait_for(diff_started.wait(), 10)
        closing = asyncio.create_task(tools.aclose())
        await asyncio.sleep(0)
        assert tools._closed and not closing.done()
        release.set()
        assert (await review).outcome == "succeeded"
        await closing
        with pytest.raises(KernelError) as closed:
            await tools.execute(review_call, CancelToken())
        assert closed.value.code == "tool_runtime_closed"
    finally:
        release.set()
        await asyncio.gather(
            review,
            *([closing] if closing is not None else []),
            return_exceptions=True,
        )
        if not tools._closed:
            await tools.aclose()


@pytest.mark.parametrize("target", ["worktree", "staged"])
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_git_diff_full_object_identity_does_not_depend_on_abbreviation(
    tmp_path: Path, target: str, object_format: str
) -> None:
    """相同镜像的完整观察不因本地对象名缩写宽度不同而漂移。"""
    _repository(tmp_path, object_format=object_format)
    (tmp_path / "tracked.py").write_text("after\n", encoding="utf-8")
    if target == "staged":
        _command(tmp_path, "add", "tracked.py")
    outputs = []
    for width in (7, 8):
        _command(tmp_path, "config", "core.abbrev", str(width))
        async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
            result = await execute(tools, "git_diff", target=target)
        assert result.outcome == "succeeded"
        assert not result.output["truncated"]
        outputs.append(result.output)
    assert outputs[0]["text"] == outputs[1]["text"]
    assert outputs[0]["observed_sha256"] == outputs[1]["observed_sha256"]
    assert outputs[0]["observed_bytes"] == outputs[1]["observed_bytes"]
    index = next(line for line in outputs[0]["text"].splitlines() if line.startswith("index "))
    before, after = index.split()[1].split("..")
    for expected, arguments in (
        (before, ("rev-parse", "HEAD:tracked.py")),
        (after, ("hash-object", "--", "tracked.py")),
    ):
        process = await asyncio.create_subprocess_exec(
            str(_git()),
            *arguments,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        output, _ = await process.communicate()
        assert process.returncode == 0
        assert expected == output.decode("ascii").strip()


@pytest.mark.parametrize("name", ["git_status", "git_diff", "git_review"])
async def test_git_full_index_policy_changes_capability_and_rejects_old_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """观察策略变化进入原能力指纹，旧版本请求不得静默继续。"""
    _repository(tmp_path)
    current_contract = GitReadRuntime.contract

    def legacy_contract(runtime: GitReadRuntime) -> dict[str, object]:
        contract = current_contract(runtime)
        contract.pop("full_index", None)
        return contract

    with monkeypatch.context() as patch:
        patch.setattr(GitReadRuntime, "contract", legacy_contract)
        async with CodingToolRuntime(tmp_path, git_executable=_git()) as legacy:
            old_call = call(legacy, name)
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        current_call = call(tools, name)
        assert current_call.tool_version != old_call.tool_version
        assert current_call.tool_fingerprint != old_call.tool_fingerprint
        port = AsyncMock(side_effect=AssertionError("旧能力不得进入Git端口"))
        with monkeypatch.context() as patch:
            patch.setattr(GitReadRuntime, "execute", port)
            with pytest.raises(KernelError, match="工具或工作区能力已变化") as refused:
                await tools.execute(old_call, CancelToken())
            port.assert_not_awaited()
        assert refused.value.code == "tool_contract_changed"
        result = await tools.execute(current_call, CancelToken())
        assert result.outcome == "succeeded"


async def test_git_disables_external_diff_and_fsmonitor(tmp_path: Path) -> None:
    _repository(tmp_path)
    marker = tmp_path / "executed"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\nprintf x > {marker!s}\n", encoding="utf-8")
    helper.chmod(0o700)
    _command(tmp_path, "config", "diff.external", str(helper))
    _command(tmp_path, "config", "core.fsmonitor", str(helper))
    (tmp_path / "tracked.py").write_text("after\n", encoding="utf-8")
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        assert (await execute(tools, "git_status")).outcome == "succeeded"
        assert (await execute(tools, "git_diff")).outcome == "succeeded"
    assert not marker.exists()


async def test_git_rejects_parent_repository_and_invalid_contract(tmp_path: Path) -> None:
    _repository(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    async with CodingToolRuntime(child, git_executable=_git()) as tools:
        denied = await execute(tools, "git_status")
        invalid = await tools.execute(call(tools, "git_diff", context_lines=True), CancelToken())
    assert denied.error.code == "tool_path_denied"
    assert invalid.error.code == "tool_invalid_arguments"
    assert str(tmp_path) not in denied.model_dump_json()


@pytest.mark.skipif(os.name != "posix", reason="Git进程绑定当前仅支持POSIX")
async def test_git_non_repository_is_bounded_failure(tmp_path: Path) -> None:
    async with CodingToolRuntime(tmp_path, git_executable=_git()) as tools:
        result = await execute(tools, "git_status")
    assert result.outcome == "failed" and result.error.code == "tool_not_found"
