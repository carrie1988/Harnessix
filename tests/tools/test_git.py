from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
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


def _repository(root: Path) -> None:
    _command(root, "init", "-q")
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
        assert {"git_status", "git_diff"} <= definitions.keys()
        assert all(
            definitions[name].effect_class.value == "read_only"
            and not definitions[name].requires_approval
            for name in ("git_status", "git_diff")
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
