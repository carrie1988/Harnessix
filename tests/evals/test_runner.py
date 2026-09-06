from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent
from harnessix.evals.catalog import historical_coding_eval
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.run_state import read_eval_run_state
from harnessix.evals.runner import run_historical_coding_eval
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "harnessix-openai-empty-incremental-call-id"
CHANGED_PATH = "src/harnessix/models/_chat_stream.py"


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git 不可用")
    return Path(executable).resolve()


def command(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return result.stdout.strip()


def environment() -> CodingEvalEnvironment:
    return CodingEvalEnvironment(
        harnessix_revision=command(ROOT, "rev-parse", "HEAD"),
        provider="scripted-eval-driver",
        model="deterministic-v1",
        platform=sys.platform,
        isolation="private-managed-copy-no-os-sandbox",
    )


def _tool_for_result(request: ModelRequest, result: ToolResultContent) -> str:
    return next(
        item.content.tool
        for item in request.history
        if getattr(item.content, "call_id", None) == result.call_id
        and getattr(item.content, "kind", None) == "tool_call"
    )


def _result(request: ModelRequest, tool: str, index: int = -1) -> ToolResultContent:
    values = [
        item.content
        for item in request.history
        if isinstance(item.content, ToolResultContent)
        and _tool_for_result(request, item.content) == tool
    ]
    return values[index]


class HistoricalFixProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request.model_copy(deep=True))
        cancel.checkpoint()
        if request.step == 1:
            tool, arguments = "run_tests", {"profile": "focused"}
        elif request.step == 2:
            failed = _result(request, "run_tests")
            assert failed.output["passed"] is False and failed.output["returncode"] == 1
            tool, arguments = "read_file", {"path": CHANGED_PATH}
        elif request.step == 3:
            source = _result(request, "read_file").output
            assert "if part.id is not None:" in source["text"]
            tool, arguments = (
                "apply_patch",
                {
                    "path": CHANGED_PATH,
                    "expected_revision": source["revision"],
                    "edits": [
                        {
                            "old_text": "if part.id is not None:",
                            "new_text": 'if part.id not in (None, ""):',
                        }
                    ],
                },
            )
        elif request.step == 4:
            patch = _result(request, "apply_patch")
            assert patch.patch is not None and patch.patch.state == "applied"
            tool, arguments = "run_tests", {"profile": "focused"}
        elif request.step == 5:
            passed = _result(request, "run_tests")
            assert passed.output["passed"] is True and passed.output["returncode"] == 0
            tool, arguments = "git_status", {}
        elif request.step == 6:
            status = _result(request, "git_status").output
            assert status["total_entries"] == 1
            assert status["entries"][0]["path"] == CHANGED_PATH
            tool, arguments = "git_diff", {"target": "worktree", "context_lines": 1}
        else:
            assert request.step == 7
            diff = _result(request, "git_diff").output["text"]
            assert "-            if part.id is not None:" in diff
            assert '+            if part.id not in (None, ""): ' not in diff
            assert '+            if part.id not in (None, ""):' in diff
            answer = json.dumps(
                {
                    "summary": "兼容空调用ID占位，同时保留真实身份漂移校验",
                    "changed_paths": [CHANGED_PATH],
                    "tests": [{"profile": "focused", "passed": True}],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield ResponseStarted(response_id="historical-fix-7")
            yield TextStarted(content_id="answer")
            yield TextCompleted(content_id="answer", text=answer)
            yield ResponseCompleted()
            return
        yield ResponseStarted(response_id=f"historical-fix-{request.step}")
        yield ToolCallCompleted(
            call_id=f"historical-fix-call-{request.step}",
            tool=tool,
            arguments=arguments,
        )
        yield ResponseCompleted(finish_reason="tool_calls")


class NeverProvider:
    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        del request, cancel
        raise AssertionError("已持久完成或取消的运行不得再次调用Provider")
        yield ResponseCompleted()


class CancellingProvider:
    def __init__(self, outer: CancelToken) -> None:
        self.outer = outer
        self.calls = 0

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        del request, cancel
        self.calls += 1
        self.outer.cancel()
        await asyncio.Event().wait()
        yield ResponseCompleted()


class ForbiddenPatchProvider:
    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        if request.step == 1:
            tool, arguments = "read_file", {"path": "README.md"}
        else:
            assert request.step == 2
            source = _result(request, "read_file").output
            assert source["text"].startswith("# Harnessix")
            tool, arguments = (
                "apply_patch",
                {
                    "path": "README.md",
                    "expected_revision": source["revision"],
                    "edits": [{"old_text": "# Harnessix", "new_text": "# Forbidden"}],
                },
            )
        yield ResponseStarted(response_id=f"forbidden-{request.step}")
        yield ToolCallCompleted(
            call_id=f"forbidden-call-{request.step}", tool=tool, arguments=arguments
        )
        yield ResponseCompleted(finish_reason="tool_calls")


async def run(tmp_path: Path, run_id: UUID, provider, **kwargs):
    return await run_historical_coding_eval(
        ROOT,
        tmp_path,
        git_executable(),
        Path(sys.executable),
        historical_coding_eval(TASK_ID),
        run_id,
        provider,
        environment(),
        **kwargs,
    )


async def test_real_history_runs_through_runtime_worker_approvals_and_grader(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    provider = HistoricalFixProvider()
    source_before = (ROOT / CHANGED_PATH).read_bytes()

    result = await run(tmp_path, run_id, provider)

    assert result.report.outcome == "passed"
    assert not result.report.failure_categories
    assert result.report.metrics.model_steps == 7
    assert result.report.metrics.tool_calls == 6
    assert result.report.metrics.approvals == 3
    assert result.report.metrics.changed_files == 1
    assert result.report.git.changed_paths == (CHANGED_PATH,)
    assert len(provider.requests) == 7
    assert result.state.status == "completed" and result.state.report_sha256 is not None
    assert (tmp_path / str(run_id) / "run-state.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / str(run_id) / "report.json").stat().st_mode & 0o777 == 0o600
    assert "if part.id is not None:" in (
        tmp_path / str(run_id) / "workspace" / CHANGED_PATH
    ).read_text(encoding="utf-8")
    assert 'if part.id not in (None, ""):' in (result.workspace / CHANGED_PATH).read_text(
        encoding="utf-8"
    )
    assert (ROOT / CHANGED_PATH).read_bytes() == source_before
    with sqlite3.connect(tmp_path / str(run_id) / "effects.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 2

    reopened = await run(tmp_path, run_id, NeverProvider())
    assert reopened.report == result.report
    assert reopened.state == result.state
    assert reopened.workspace == result.workspace

    state_path = tmp_path / str(run_id) / "run-state.json"
    state_path.chmod(0o644)
    with pytest.raises(KernelError) as error:
        read_eval_run_state(state_path)
    assert error.value.code == "eval_run_state_invalid"


async def test_reopens_after_process_approval_without_replaying_action(tmp_path: Path) -> None:
    run_id = uuid4()
    provider = HistoricalFixProvider()
    crashed = False

    def fault(point: str) -> None:
        nonlocal crashed
        if point == "eval_runner.after_process_approval" and not crashed:
            crashed = True
            raise RuntimeError("simulated host exit after durable approval")

    with pytest.raises(RuntimeError, match="simulated host exit"):
        await run(tmp_path, run_id, provider, fault=fault)

    state = read_eval_run_state(tmp_path / str(run_id) / "run-state.json")
    assert state.status == "running" and state.thread_id is not None and state.turn_id is not None
    assert not (tmp_path / str(run_id) / "report.json").exists()
    assert len(provider.requests) == 1

    recovered = await run(tmp_path, run_id, provider)
    assert recovered.report.outcome == "passed"
    assert len(provider.requests) == 7
    with sqlite3.connect(tmp_path / str(run_id) / "effects.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 2


async def test_cancellation_is_durable_and_reopen_grades_terminal_turn(tmp_path: Path) -> None:
    run_id = uuid4()
    token = CancelToken()
    provider = CancellingProvider(token)

    with pytest.raises(TurnCancelled):
        await run(tmp_path, run_id, provider, cancel=token)

    state = read_eval_run_state(tmp_path / str(run_id) / "run-state.json")
    assert state.status == "running" and state.thread_id is not None and state.turn_id is None
    assert provider.calls == 1
    assert not (tmp_path / str(run_id) / "report.json").exists()

    recovered = await run(tmp_path, run_id, NeverProvider())
    assert recovered.state.status == "completed"
    assert recovered.report.outcome == "failed"
    assert "runtime" in recovered.report.failure_categories
    assert recovered.report.git.changed_paths == ()


async def test_report_publication_is_recovered_without_regrading(tmp_path: Path) -> None:
    run_id = uuid4()
    provider = HistoricalFixProvider()

    def fault(point: str) -> None:
        if point == "eval_runner.after_report":
            raise RuntimeError("simulated host exit after report publication")

    with pytest.raises(RuntimeError, match="after report publication"):
        await run(tmp_path, run_id, provider, fault=fault)

    run_root = tmp_path / str(run_id)
    state = read_eval_run_state(run_root / "run-state.json")
    assert state.status == "running" and state.turn_id is not None
    assert (run_root / "report.json").is_file()
    assert len(provider.requests) == 7

    recovered = await run(tmp_path, run_id, NeverProvider())
    assert recovered.state.status == "completed"
    assert recovered.report.outcome == "passed"


async def test_task_allowlist_rejects_patch_to_other_managed_file(tmp_path: Path) -> None:
    run_id = uuid4()

    with pytest.raises(KernelError) as error:
        await run(tmp_path, run_id, ForbiddenPatchProvider())

    assert error.value.code == "eval_approval_denied"
    run_root = tmp_path / str(run_id)
    state = read_eval_run_state(run_root / "run-state.json")
    workspace = run_root / "managed" / str(state.execution_workspace_id) / "workspace"
    assert (workspace / "README.md").read_text(encoding="utf-8").startswith("# Harnessix")
    assert not (run_root / "report.json").exists()
    with sqlite3.connect(run_root / "effects.sqlite") as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


async def test_provision_rejects_unpinned_host_only_paths(tmp_path: Path) -> None:
    run_id = uuid4()
    definition = replace(historical_coding_eval(TASK_ID), host_only_paths=())

    with pytest.raises(KernelError) as error:
        await run_historical_coding_eval(
            ROOT,
            tmp_path,
            git_executable(),
            Path(sys.executable),
            definition,
            run_id,
            NeverProvider(),
            environment(),
        )

    assert error.value.code == "eval_execution_workspace_failed"
    run_root = tmp_path / str(run_id)
    assert not (run_root / "managed").exists()
    assert not (run_root / "run-state.json").exists()
