from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.contracts import ProcessRequest
from harnessix.processes.git_read_windows import (
    WindowsGitReadProcess,
    _environment,
    _git_read_plan,
    reconcile_windows_git_reads,
)
from harnessix.processes.owner_receipt import read_owner_receipt
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import WindowsProcessSupervisor
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.workspace.git_windows_binding import validate_windows_git_executable
from tests.processes.test_windows_supervisor import _wait_stopped
from tests.tools.test_files import execute

pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实Windows Git/Job Object契约")


def _git() -> Path:
    value = shutil.which("git.exe")
    assert value is not None, "Windows原生功能验收需要Git for Windows"
    return Path(value).absolute()


def _command(root: Path, *arguments: str) -> bytes:
    system_root = os.environ["SystemRoot"]
    result = subprocess.run(
        (str(_git()), "-c", "core.autocrlf=false", "-c", "core.hooksPath=NUL", *arguments),
        cwd=root,
        check=True,
        timeout=10,
        capture_output=True,
        env={
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": str(_git().parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "NUL",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
        },
    )
    return result.stdout


def _repository(root: Path) -> None:
    root.mkdir()
    _command(root, "init", "-q")
    _command(root, "config", "user.name", "Harnessix Test")
    _command(root, "config", "user.email", "test@harnessix.invalid")
    (root / "main.py").write_bytes(b"before\n")
    _command(root, "add", "main.py")
    _command(root, "commit", "-qm", "baseline")


def _leases(state: Path) -> list[ProcessLease]:
    db = sqlite3.connect(state / "process-owner/process-leases.db")
    try:
        rows = db.execute("SELECT payload FROM process_leases ORDER BY rowid").fetchall()
    finally:
        db.close()
    return [ProcessLease.model_validate_json(row[0]) for row in rows]


def _owner_shutdown_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """只在原生故障回归中输出无局部变量的Owner栈，不改变控制句柄与Job启动。"""
    original = subprocess.Popen
    diagnostic = (
        "import faulthandler,runpy;"
        "faulthandler.dump_traceback_later(3,repeat=True);"
        "runpy.run_module('harnessix.processes.windows_owner',run_name='__main__')"
    )

    def traced_owner(arguments, *args, **kwargs):
        if tuple(arguments[1:3]) == ("-m", "harnessix.processes.windows_owner"):
            arguments = (arguments[0], "-c", diagnostic, *arguments[3:])
            kwargs["stderr"] = None
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", traced_owner)


async def test_windows_git_unicode_status_staged_diff_and_persistent_receipts(
    tmp_path: Path,
) -> None:
    root, state = tmp_path / "工程 空格", tmp_path / "private-state"
    _repository(root)
    (root / "main.py").write_bytes("中间\n".encode())
    _command(root, "add", "main.py")
    (root / "main.py").write_bytes("最后\n".encode())
    (root / "新增.txt").write_bytes(b"new\n")
    index_before = (root / ".git/index").read_bytes()
    async with CodingToolRuntime(root, git_executable=_git(), git_state_directory=state) as tools:
        status = await execute(tools, "git_status")
        staged = await execute(tools, "git_diff", target="staged", context_lines=0)
        worktree = await execute(tools, "git_diff", target="worktree", context_lines=0)
        assert status.outcome == staged.outcome == worktree.outcome == "succeeded"
        assert status.output["total_entries"] == 2
        assert "+中间" in staged.output["text"] and "+最后" in worktree.output["text"]
    assert (root / ".git/index").read_bytes() == index_before
    leases = _leases(state)
    assert len(leases) == 9
    assert all(lease.state == "exited" and lease.returncode == 0 for lease in leases)
    assert all(lease.stdout.eof and lease.stderr.eof for lease in leases)
    assert len({lease.process_id for lease in leases}) == 9


async def test_windows_git_rejects_parent_repository_and_preserves_nonrepo_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _repository(root)
    child, empty = root / "child", tmp_path / "empty"
    child.mkdir()
    empty.mkdir()
    for workspace, code in ((child, "tool_path_denied"), (empty, "tool_not_found")):
        async with CodingToolRuntime(
            workspace,
            git_executable=_git(),
            git_state_directory=tmp_path / f"state-{workspace.name}",
        ) as tools:
            result = await execute(tools, "git_status")
        facts = _leases(tmp_path / f"state-{workspace.name}")
        observed = [(item.state, item.stop_reason, item.returncode) for item in facts]
        assert result.error.code == code, observed
        assert str(workspace) not in result.model_dump_json()


@pytest.mark.parametrize("key", ["filter.fixture.clean", "filter.fixture.process", "include.path"])
async def test_windows_git_never_executes_repository_helpers(tmp_path: Path, key: str) -> None:
    root, state = tmp_path / "repo", tmp_path / "state"
    _repository(root)
    marker = root / "unexpected-helper"
    helper = f"cmd.exe /d /c echo x > {marker.as_posix()}"
    if key == "include.path":
        included = tmp_path / "included.cfg"
        included.write_text('[filter "fixture"]\n\tclean = ' + helper + "\n", encoding="utf-8")
        value = included.as_posix()
    else:
        value = helper
    _command(root, "config", key, value)
    (root / ".gitattributes").write_bytes(b"main.py filter=fixture\n")
    (root / "main.py").write_bytes(b"after\n")
    async with CodingToolRuntime(root, git_executable=_git(), git_state_directory=state) as tools:
        result = await execute(tools, "git_diff")
    assert result.error.code == "tool_path_denied"
    assert not marker.exists()


async def test_windows_git_requires_external_state_and_rejects_relative_or_script_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(KernelError) as missing:
        CodingToolRuntime(tmp_path, git_executable=_git())
    assert missing.value.code == "product_git_state_required"
    with pytest.raises(KernelError) as overlap:
        CodingToolRuntime(tmp_path, git_executable=_git(), git_state_directory=tmp_path / "state")
    assert overlap.value.code == "product_state_overlap"
    for path in (Path("git.exe"), tmp_path / "git.cmd"):
        with pytest.raises(KernelError) as invalid:
            validate_windows_git_executable(path)
        assert invalid.value.code == "product_git_invalid"


async def test_windows_git_diff_prefix_keeps_complete_observation(tmp_path: Path) -> None:
    root, state = tmp_path / "repo", tmp_path / "state"
    _repository(root)
    (root / "main.py").write_bytes(("中文差异" * 30_000 + "\n").encode())
    async with CodingToolRuntime(root, git_executable=_git(), git_state_directory=state) as tools:
        result = await execute(tools, "git_diff")
    assert result.outcome == "succeeded"
    assert result.output["truncated"] is True
    assert result.output["utf8_bytes"] <= 48 * 1024 < result.output["observed_bytes"]
    assert len(result.output["observed_sha256"]) == 64


async def test_windows_git_recovery_reconciles_original_receipt_without_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _owner_shutdown_diagnostics(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    root.mkdir()
    executable = Path(sys.executable)
    driver = WindowsGitReadProcess(root, executable, state)
    config = driver._configuration
    spec = build_process_spec(
        invocation="argv",
        argv=(str(executable), "-I", "-c", "import time;time.sleep(0.5);print('done')"),
        timeout_seconds=5.0,
    )
    environment = _environment(executable)
    plan = _git_read_plan(config, spec, environment)
    with SQLiteExecutionPlanStore(state / "execution-plans.db") as plans:
        plans.save_plan(plan)
    async with WindowsProcessSupervisor(state / "process-owner") as supervisor:
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=root,
            environment=environment,
        )
        assert handle.lease.state == "running"
        for _ in range(500):
            receipt = read_owner_receipt(
                state / "process-owner/runs" / str(spec.process_id) / "receipt.json",
                process_id=spec.process_id,
                owner_token=handle.lease.owner_token,
                owner_identity=handle.lease.owner_identity,
            )
            if receipt is not None and receipt.state == "exited":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("原Owner未发布签名终态")
        # 不刷新原Controller，SQLite仍保留真实running，模拟丢失最后一次观察。
        assert _leases(state)[0].state == "running"
        # Owner必须独立退出，不能要求仍在线的Controller先刷新Receipt或关闭控制FD。
        owner = handle._owner
        assert owner is not None
        try:
            assert await asyncio.to_thread(owner.wait, timeout=8) == 0
        finally:
            # 即使退出断言失败，也关闭原控制写端并有界回收，避免测试自身无限挂起。
            handle._close_control()
            await asyncio.to_thread(owner.wait, timeout=5)
            supervisor._handles.pop(spec.process_id)
        await handle._reap_owner()
        await reconcile_windows_git_reads(state)
        assert _leases(state)[0].state == "exited"
        assert len(_leases(state)) == 1
        assert len(list((state / "process-owner/runs").iterdir())) == 1


@pytest.mark.parametrize("mode", ["timeout", "token", "task", "during_launch"])
async def test_windows_git_owner_timeout_and_cancellation_leave_no_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    root, state = tmp_path / "workspace", tmp_path / "state"
    root.mkdir()
    marker = root / "started"
    request = ProcessRequest(
        program="git",
        timeout_seconds=2.0 if mode == "timeout" else 5.0,
        arguments=(
            "-I",
            "-c",
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
            # 启动期取消可发生在truncate与write之间；标记只在完整写入后原子发布。
            "marker=pathlib.Path(sys.argv[1]); temporary=marker.with_suffix('.tmp'); "
            "temporary.write_text(str(child.pid)); temporary.replace(marker); time.sleep(30)",
            str(marker),
        ),
    )
    entered = asyncio.Event()
    if mode == "during_launch":
        original = WindowsProcessSupervisor._spawn_owner

        def slow_launch(self: WindowsProcessSupervisor, fd: int, directory: Path):
            import time

            time.sleep(0.3)
            return original(self, fd, directory)

        monkeypatch.setattr(WindowsProcessSupervisor, "_spawn_owner", slow_launch)
        original_start = WindowsProcessSupervisor.start

        async def observe_start(self: WindowsProcessSupervisor, *args, **kwargs):
            entered.set()
            return await original_start(self, *args, **kwargs)

        monkeypatch.setattr(WindowsProcessSupervisor, "start", observe_start)
    token = CancelToken()
    async with WindowsGitReadProcess(root, Path(sys.executable), state) as driver:
        task = asyncio.create_task(driver.run(request, token))
        if mode == "during_launch":
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
        elif mode != "timeout":
            for _ in range(500):
                if marker.exists():
                    break
                if task.done():
                    pytest.fail("Owner在写入启动标记前失败")
                await asyncio.sleep(0.01)
            assert marker.exists()
            token.cancel() if mode == "token" else task.cancel()
        expected = (
            ReadToolError
            if mode == "timeout"
            else TurnCancelled
            if mode == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=12)
    leases = _leases(state)
    assert len(leases) == 1
    assert leases[0].state == "exited"
    assert leases[0].stop_reason in {"timeout", "cancelled"}
    assert leases[0].stdout.eof and leases[0].stderr.eof
    assert leases[0].pid is not None
    await _wait_stopped(leases[0].pid)
    if marker.exists():
        await _wait_stopped(int(marker.read_text()))
