from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.contracts import MAX_CAPTURE_BYTES, ProcessRequest
from harnessix.processes.git_read_windows import (
    WindowsGitReadProcess,
    _environment,
    _git_read_plan,
    reconcile_windows_git_reads,
)
from harnessix.processes.owner_receipt import (
    ProcessOwnerReceipt,
    ProcessOwnerReceiptV2,
    read_owner_receipt,
    sign_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import SupervisedProcess, WindowsProcessSupervisor
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.git_baseline import _Queries
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime, _reject_git_helpers
from harnessix.tools.git_contracts import GitStatusInput
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.workspace.git_windows_binding import (
    pin_windows_git,
    validate_windows_git_executable,
)
from tests.agent.test_publication import CANARY, protected
from tests.processes.child_ready import CHILD_READY_PROGRAM
from tests.processes.test_windows_supervisor import _wait_stopped
from tests.product_config.test_git_baseline import collect, repository
from tests.product_config.test_git_delivery_source import edit
from tests.product_config.test_product_patch_rollback import product
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


@pytest.mark.parametrize("baseline", [False, True], ids=["ordinary", "raw-baseline"])
@pytest.mark.parametrize("mode", ["timeout", "token", "task", "during_launch"])
async def test_windows_git_owner_timeout_and_cancellation_leave_no_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    baseline: bool,
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
            CHILD_READY_PROGRAM,
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
    async with WindowsGitReadProcess(
        root, Path(sys.executable), state, for_delivery=baseline
    ) as driver:
        entrypoint = driver.run_baseline if baseline else driver.run
        task = asyncio.create_task(entrypoint(request, token))
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
        with pytest.raises(expected) as denied:
            await asyncio.wait_for(task, timeout=12)
        if mode == "timeout":
            assert denied.value.code == "timeout"
    leases = _leases(state)
    assert len(leases) == 1
    assert leases[0].state == "exited"
    assert leases[0].stop_reason in {"timeout", "cancelled"}
    assert leases[0].stop_reason == ("timeout" if mode == "timeout" else "cancelled")
    assert leases[0].stdout.eof and leases[0].stderr.eof
    assert leases[0].pid is not None
    await _wait_stopped(leases[0].pid)
    receipt = read_owner_receipt(
        state / "process-owner/runs" / str(leases[0].process_id) / "receipt.json",
        process_id=leases[0].process_id,
        owner_identity=leases[0].owner_identity,
        owner_token=leases[0].owner_token,
    )
    assert isinstance(receipt, ProcessOwnerReceiptV2)
    assert receipt.stop_reason == leases[0].stop_reason
    assert receipt.raw_stdout.eof and receipt.raw_stderr.eof
    if baseline and mode != "during_launch":
        assert marker.exists(), "基准拒绝必须覆盖已实际启动的目标和子进程"
    if marker.exists():
        # 就绪标记创建前PID正文已经关闭；只读取已发布的完整PID，不重试或忽略空值。
        await _wait_stopped(int(marker.with_name(marker.name + ".pid").read_text()))


def test_windows_git_readiness_publication_with_pinned_workspace(tmp_path, capsys):
    """原生核对路径Rename与正文后就绪发布；诊断只公开类别和数值码。"""
    root = tmp_path / "workspace"
    root.mkdir()
    code = """
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
temporary=root/'legacy.tmp'
phase='write'
try:
    temporary.write_text('123456',encoding='ascii')
    phase='replace'
    temporary.replace(root/'legacy')
    legacy={'status':'passed','phase':phase,'error_type':None,'errno':None,'winerror':None}
except OSError as error:
    legacy={'status':'failed','phase':phase,'error_type':type(error).__name__,
            'errno':error.errno,'winerror':getattr(error,'winerror',None)}
phase='pid_write'
try:
    pid_file=root/'started.pid'
    pid_file.write_text('123456',encoding='ascii')
    marker=root/'started'
    phase='ready_create'
    marker.touch(exist_ok=False)
    complete=marker.read_bytes()==b'' and pid_file.read_bytes()==b'123456'
    error_facts=None
except OSError as error:
    complete=False
    error_facts={'phase':phase,'error_type':type(error).__name__,
                 'errno':error.errno,'winerror':getattr(error,'winerror',None)}
print(json.dumps({'legacy_replace':legacy,'closed_pid_then_ready':complete,
                  'ready_error':error_facts}))
"""
    with pin_windows_git(root, Path(sys.executable)):
        result = subprocess.run(
            (sys.executable, "-I", "-c", code, str(root)),
            env=_environment(Path(sys.executable)),
            capture_output=True,
            timeout=10,
        )
    assert result.returncode == 0
    facts = json.loads(result.stdout)
    # 原Rename结果仅用于定位，不将平台是否拒绝路径式发布当作产品成功标准。
    with capsys.disabled():
        print("windows_marker_publication=" + json.dumps(facts, sort_keys=True))
    assert facts["closed_pid_then_ready"] is True
    assert facts["ready_error"] is None
    assert facts["legacy_replace"]["status"] in {"passed", "failed"}


async def test_windows_git_protected_large_blob_and_unparsed_index_status_use_raw(tmp_path):
    """真实Git和认证Patch生成基准；全量Index/Status不解析其脱敏正文。"""
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        size = MAX_CAPTURE_BYTES + 4096
        prefix = b"\0LF\nCRLF\r\nCTRL-Z\x1a\xff" + CANARY.encode()
        tail = CANARY.encode() + b"\x80END"
        body = prefix + b"x" * (size - len(prefix) - len(tail)) + tail
        (root / "src/modified.py").write_bytes(body)
        unrelated = root / "src/model-value/+version-9"
        unrelated.parent.mkdir()
        unrelated.write_bytes(b"clean\n")
        repository(root)
        unrelated.write_bytes(b"unrelated dirty\n")
        original_index = (root / ".git/index").read_bytes()
        original_head = _command(root, "rev-parse", "HEAD")
        thread = await runtime.create_thread(str(root))
        target = await edit(
            runtime,
            provider,
            thread.thread_id,
            before=body,
            after=b"new\n",
            request="native-protected-large-blob",
        )
        state = tmp_path / "git-state"
        with protected() as scope:
            reader = GitReadRuntime(
                root, _git(), state_directory=state, output_redaction=scope, for_delivery=True
            )
            baseline = await collect(
                runtime, thread.thread_id, target, router, transactions, reader
            )
            cancel = CancelToken()
            blob = await reader._run_baseline(
                (*reader._global_arguments, "cat-file", "blob", baseline.members[0].oid), cancel
            )
            index = await reader._run_baseline(
                (*reader._global_arguments, "ls-files", "--stage", "--debug", "-z"), cancel
            )
            status = await reader._run_baseline(
                (
                    *reader._global_arguments,
                    "status",
                    "--porcelain=v2",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    "-z",
                ),
                cancel,
            )
        expected_safe = body.replace(CANARY.encode(), b"[REDACTED]")
        assert baseline.source.mutations[0].before.size == blob.raw_stdout.observed_bytes == size
        assert (
            baseline.source.mutations[0].before.sha256
            == blob.raw_stdout.sha256
            == hashlib.sha256(body).hexdigest()
        )
        assert blob.result.stdout.data() == expected_safe[:MAX_CAPTURE_BYTES]
        assert (
            blob.result.stdout.captured_bytes == MAX_CAPTURE_BYTES and blob.result.stdout.truncated
        )
        assert blob.result.stdout.observed_bytes == len(expected_safe)
        assert blob.result.stdout.observed_sha256 == hashlib.sha256(expected_safe).hexdigest()
        assert blob.result.stdout.observed_sha256 != blob.raw_stdout.sha256
        assert CANARY not in baseline.model_dump_json()
        assert baseline.index_observation_sha256 == index.raw_stdout.sha256
        assert baseline.index_observation_bytes == index.raw_stdout.observed_bytes
        assert baseline.status_sha256 == status.raw_stdout.sha256
        for observation in (index, status):
            assert b"[REDACTED]" in observation.result.stdout.data()
            assert observation.raw_stdout.sha256 != observation.result.stdout.observed_sha256
            assert observation.result.stdout.eof and observation.result.stderr.eof
            assert observation.raw_stdout.eof and observation.raw_stderr.eof
        assert (root / ".git/index").read_bytes() == original_index
        assert _command(root, "rev-parse", "HEAD") == original_head
        assert unrelated.read_bytes() == b"unrelated dirty\n"
        leases = _leases(state)
        assert leases and all(lease.state == "exited" and lease.returncode == 0 for lease in leases)
        assert all(lease.stop_reason == "exited" for lease in leases)
        assert all(lease.stdout.eof and lease.stderr.eof for lease in leases)
        for lease in leases:
            assert lease.pid is not None
            await _wait_stopped(lease.pid)
        for path in (state / "process-owner/runs").glob("*/*.bin"):
            assert CANARY.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "metadata",
    [
        "root",
        "config",
        "head",
        "tree",
        "ref",
        "selected-tree",
        "selected-stage",
        "selected-flags",
        "selected-debug",
    ],
)
async def test_windows_git_parsed_metadata_rejects_authenticated_redaction(tmp_path, metadata):
    """原根和配置守卫照常执行；只对真实解析入口验证raw与安全正文一致性。"""
    root, state = tmp_path / "repo", tmp_path / "state"
    _repository(root)
    _command(root, "config", "fixture.synthetickey", "benign")
    head = _command(root, "rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
    tree = _command(root, "rev-parse", "--verify", head + "^{tree}").strip().decode("ascii")
    ref = _command(root, "rev-parse", "--symbolic-full-name", "HEAD").strip().decode("ascii")
    value = {
        "root": root.name,
        "config": "fixture.synthetickey",
        "head": head,
        "tree": tree,
        "ref": ref,
    }.get(metadata, "main.py")
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("fixture", "1", "SYNTHETIC_OUTPUT_VALUE"),),
        environment={"SYNTHETIC_OUTPUT_VALUE": value},
    )
    original_index = (root / ".git/index").read_bytes()
    queries = {
        "head": ("rev-parse", "--verify", "HEAD^{commit}"),
        "tree": ("rev-parse", "--verify", head + "^{tree}"),
        "ref": ("rev-parse", "--symbolic-full-name", "HEAD"),
        "selected-tree": ("ls-tree", "-z", "--full-tree", tree, "--", "main.py"),
        "selected-stage": ("ls-files", "--stage", "-z", "--", "main.py"),
        "selected-flags": ("ls-files", "-v", "-z", "--", "main.py"),
        "selected-debug": ("ls-files", "--debug", "-z", "--", "main.py"),
    }
    with SecretPublicationScope((SecretReference(name="fixture", version="1"),), provider) as scope:
        reader = GitReadRuntime(
            root, _git(), state_directory=state, output_redaction=scope, for_delivery=True
        )
        cancel = CancelToken()
        with pytest.raises(KernelError) as denied:
            await reader._require_repository_root(cancel)
            await _reject_git_helpers(reader, cancel)
            await _Queries(reader, cancel).full(*queries[metadata])
        assert denied.value.code == "git_baseline_metadata_changed"
    leases = _leases(state)
    assert len(leases) == (1 if metadata == "root" else 2 if metadata == "config" else 3)
    assert all(lease.state == "exited" and lease.returncode == 0 for lease in leases)
    assert all(lease.stdout.eof and lease.stderr.eof for lease in leases)
    lease = leases[-1]
    directory = state / "process-owner/runs" / str(lease.process_id)
    receipt = read_owner_receipt(
        directory / "receipt.json",
        process_id=lease.process_id,
        owner_identity=lease.owner_identity,
        owner_token=lease.owner_token,
    )
    assert isinstance(receipt, ProcessOwnerReceiptV2)
    assert receipt.raw_stdout.eof and receipt.raw_stderr.eof
    assert receipt.raw_stdout.sha256 != receipt.stdout.sha256
    safe = (directory / "stdout.bin").read_bytes()
    assert b"[REDACTED]" in safe and value.encode() not in safe
    assert (root / ".git/index").read_bytes() == original_index
    for item in leases:
        assert item.pid is not None
        await _wait_stopped(item.pid)


@pytest.mark.parametrize("with_protection", [False, True])
async def test_windows_git_authenticated_historical_v1_never_supplies_raw_baseline(
    tmp_path, monkeypatch, with_protection
):
    """真实Owner完成后组装已知安全的历史v1夹具；不模拟旧Owner的原生执行。"""
    root, state = tmp_path / "repo", tmp_path / "state"
    _repository(root)
    original = SupervisedProcess._terminal_owner_receipt
    historical = []

    async def original_v1(handle):
        authenticated = await original(handle)
        assert isinstance(authenticated, ProcessOwnerReceiptV2)
        fields = (
            "process_id",
            "owner_identity",
            "state",
            "sequence",
            "pid",
            "started_at",
            "finished_at",
            "returncode",
            "stop_reason",
            "stdout",
            "stderr",
        )
        legacy = sign_owner_receipt(
            owner_token=handle.lease.owner_token,
            **{name: getattr(authenticated, name) for name in fields},
        )
        assert isinstance(legacy, ProcessOwnerReceipt)
        write_owner_receipt(handle._run_directory / "receipt.json", legacy)
        verified = await original(handle)
        assert verified == legacy
        historical.append(verified)
        return verified

    monkeypatch.setattr(SupervisedProcess, "_terminal_owner_receipt", original_v1)
    with protected() if with_protection else nullcontext() as scope:
        reader = GitReadRuntime(
            root, _git(), state_directory=state, output_redaction=scope, for_delivery=True
        )
        with pytest.raises(KernelError) as denied:
            await reader.execute(GitStatusInput(), CancelToken())
        assert denied.value.code == "git_baseline_raw_observation_required"
    assert len(historical) == 1
    assert "raw_stdout" not in historical[0].model_dump()
    assert "raw_stderr" not in historical[0].model_dump()
    leases = _leases(state)
    assert len(leases) == 1 and leases[0].state == "exited" and leases[0].returncode == 0
    assert leases[0].stdout.eof and leases[0].stderr.eof
    assert leases[0].pid is not None
    await _wait_stopped(leases[0].pid)
