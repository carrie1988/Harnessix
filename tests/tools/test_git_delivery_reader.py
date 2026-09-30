"""Git交付读取的离线契约；模拟句柄和Owner不构成原生Windows验收证据。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.processes import git_read_windows as windows_reader
from harnessix.processes.capture import CaptureProtocol
from harnessix.processes.contracts import ProcessRequest, ProcessResult, ProcessStream
from harnessix.processes.git_observation import GitBaselineReadResult
from harnessix.processes.owner_receipt import (
    RawProcessOutputObservation,
    sign_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessLease, ProcessOutputObservation
from harnessix.processes.supervision_planner import build_process_capability
from harnessix.processes.supervisor import SupervisedProcess
from harnessix.tools import git as git_reader
from harnessix.tools.git_contracts import GitDiffInput, GitStatusInput
from harnessix.workspace import snapshot as workspace_snapshot

_MIB = 1024 * 1024
_POSIX_ARGUMENTS = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "color.ui=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)
_WINDOWS_ARGUMENTS = (
    *_POSIX_ARGUMENTS[:-1],
    "core.hooksPath=NUL",
    "-c",
    "core.attributesFile=NUL",
    "-c",
    "submodule.recurse=false",
)
_POSIX_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_PUBLIC_CONTRACT = {
    "timeout_seconds": 5.0,
    "max_capture_bytes": _MIB,
    "max_diff_text_bytes": 48 * 1024,
    "max_result_bytes": 60000,
    "pager": False,
    "optional_locks": False,
    "external_diff": False,
    "textconv": False,
    "fsmonitor": False,
    "executable_filters": False,
    "configuration_includes": False,
    "submodule_queries": False,
}


@pytest.fixture(autouse=True)
def offline_process_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """污染继承环境并禁止启动子进程，避免离线案例意外变成集成验证。"""
    for name in (
        *_POSIX_ENVIRONMENT,
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_NO_LAZY_FETCH",
        "GIT_ALLOW_PROTOCOL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "SSH_ASKPASS",
        "HTTP_PROXY",
    ):
        monkeypatch.setenv(name, "offline-untrusted")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))

    def forbidden_process(*_args, **_kwargs):
        pytest.fail("离线契约测试不得启动Git、模型或其他子进程")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_process)
    monkeypatch.setattr(asyncio.BaseEventLoop, "subprocess_exec", forbidden_process)
    monkeypatch.setattr(asyncio.BaseEventLoop, "subprocess_shell", forbidden_process)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _baseline_binding(process_fingerprint: str) -> str:
    return _digest({"process_binding": process_fingerprint, "observation": "git-raw/v2"})


@pytest.fixture
def bound_python() -> Path:
    """只使用现有可执行文件构建POSIX绑定，不实际执行它。"""
    return Path(sys.executable).resolve(strict=True)


def _limits(for_delivery: bool) -> dict[str, object]:
    return {
        "max_timeout_seconds": 5.0,
        "stdout_bytes": _MIB,
        "stderr_bytes": 16 * 1024,
        "stop_output_bytes": (9 if for_delivery else 8) * _MIB,
        "terminate_grace_seconds": 0.2,
        "pipe_drain_seconds": 0.5,
    }


def _arguments(platform: str, for_delivery: bool) -> tuple[str, ...]:
    arguments = _WINDOWS_ARGUMENTS if platform == "nt" else _POSIX_ARGUMENTS
    if for_delivery:
        arguments += (
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.attributesFile=" + ("NUL" if platform == "nt" else "/dev/null"),
            "-c",
            "submodule.recurse=false",
        )
    return arguments


def _posix_fingerprint(root: Path, executable: Path, for_delivery: bool) -> str:
    root, executable = root.resolve(strict=True), executable.resolve(strict=True)
    info = executable.stat()
    environment = dict(_POSIX_ENVIRONMENT)
    if for_delivery:
        environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    # 独立重建原绑定载荷，不能仅与同一实现生成的第二个摘要比较。
    return _digest(
        {
            "implementation": "host-process-runtime/v1",
            "cwd": str(root),
            "cwd_identity": (root.stat().st_dev, root.stat().st_ino),
            "programs": {
                "git": {
                    "path": str(executable),
                    "identity": (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_mode,
                    ),
                }
            },
            "environment": environment,
            "limits": _limits(for_delivery),
        }
    )


def _windows_environment(executable: Path, for_delivery: bool) -> dict[str, str]:
    system_root = os.environ["SystemRoot"]
    environment = {
        **_POSIX_ENVIRONMENT,
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": os.pathsep.join((str(executable.parent), str(Path(system_root) / "System32"))),
        "GIT_CONFIG_GLOBAL": "NUL",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": "",
    }
    if for_delivery:
        environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


@pytest.fixture
def simulated_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """仅替换原生句柄、能力探测和快照入口，保留正式配置及Plan构建。"""
    root = tmp_path / "workspace"
    root.mkdir()
    native = Mock(fingerprint="1" * 64)
    capability = build_process_capability(
        platform="windows", supports_pty=False, implementation_digest="2" * 64
    )
    monkeypatch.setattr(windows_reader, "pin_windows_git", lambda *_args: nullcontext(native))
    monkeypatch.setattr(windows_reader, "pin_windows_git_state", lambda _path: nullcontext())
    monkeypatch.setattr(windows_reader, "probe_windows_process_capability", lambda: capability)
    observation = SimpleNamespace(
        kind="directory",
        identity=("simulated-volume", "simulated-file"),
        content=b"[]",
        size=0,
        entries=(),
    )
    snapshot_root = SimpleNamespace(
        path=root,
        root_identity=observation.identity,
        observe=lambda *_args, **_kwargs: observation,
        close=lambda: None,
    )
    monkeypatch.setattr(workspace_snapshot, "_open_native", lambda *_args: snapshot_root)
    return SimpleNamespace(
        root=root,
        executable=tmp_path / "unused-git.exe",
        state=tmp_path / "private-state",
        native=native,
        capability=capability,
        redaction=Mock(),
    )


def _windows_fingerprint(fixture: SimpleNamespace, for_delivery: bool) -> str:
    payload = {
        "native_binding": fixture.native.fingerprint,
        "capability": fixture.capability.digest,
        "executable": os.path.normcase(str(fixture.executable)),
        "state": os.path.normcase(str(fixture.state)),
    }
    if for_delivery:
        payload["purpose"] = "git-delivery-baseline/raw-v2"
    return _digest(payload)


@pytest.mark.skipif(os.name != "posix", reason="POSIX绑定构建；不启动真实Git")
@pytest.mark.parametrize(
    "delivery_mode", [None, False, True], ids=["omitted", "default", "delivery"]
)
def test_posix_profile_keeps_exact_binding_environment_arguments_and_limits(
    tmp_path: Path,
    delivery_mode: bool | None,
) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    options = {} if delivery_mode is None else {"for_delivery": delivery_mode}
    reader = git_reader.GitReadRuntime(tmp_path, executable, **options)
    process = reader._runtime()
    for_delivery = delivery_mode is True
    expected_environment = dict(_POSIX_ENVIRONMENT)
    if for_delivery:
        expected_environment.update(
            GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL=""
        )
    fingerprint = _posix_fingerprint(tmp_path, executable, for_delivery)
    assert process.binding_fingerprint == fingerprint
    assert process._environment == expected_environment
    assert process._limits.model_dump(mode="json") == _limits(for_delivery)
    assert reader._global_arguments == _arguments("posix", for_delivery)
    assert reader.contract() == {
        **_PUBLIC_CONTRACT,
        "implementation": "git-baseline-read/v1" if for_delivery else "git-read/v1",
        "binding": _baseline_binding(fingerprint) if for_delivery else fingerprint,
    }
    ordinary = git_reader.GitReadRuntime(tmp_path, executable)
    assert ordinary.contract()["binding"] == _posix_fingerprint(tmp_path, executable, False)
    assert ordinary._global_arguments == _POSIX_ARGUMENTS
    assert git_reader._ENVIRONMENT == _POSIX_ENVIRONMENT
    assert fingerprint != _posix_fingerprint(tmp_path, executable, not for_delivery)
    if for_delivery:
        assert reader.contract()["binding"] != fingerprint


@pytest.mark.skipif(os.name != "posix", reason="POSIX捕获协议；不启动真实Git")
@pytest.mark.parametrize("for_delivery", [False, True], ids=["8MiB", "9MiB"])
async def test_stream_guard_stops_at_exact_combined_budget_without_enlarging_prefixes(
    tmp_path: Path,
    bound_python: Path,
    for_delivery: bool,
) -> None:
    process = git_reader._build_git_process(
        tmp_path, bound_python, None, None, for_delivery=for_delivery
    )
    stops, closed = [], []
    capture = CaptureProtocol(process._limits, stops.append)
    pipes = {fd: Mock(close=lambda fd=fd: closed.append(fd)) for fd in (1, 2)}
    capture.transport = SimpleNamespace(get_pipe_transport=pipes.get)
    stdout, stderr = b"a" * (4 * _MIB), b"b" * (4 * _MIB - 1)
    capture.pipe_data_received(1, stdout)
    capture.pipe_data_received(2, stderr)
    assert stops == [] and closed == []
    if for_delivery:
        capture.pipe_data_received(2, b"b")
        assert stops == [] and closed == []
        capture.pipe_data_received(2, b"b" * (_MIB - 1))
        stderr += b"b" * _MIB
    assert sum(stream.observed for stream in capture.streams.values()) == (
        (9 if for_delivery else 8) * _MIB - 1
    )
    capture.pipe_data_received(2, b"b")
    stderr += b"b"
    assert stops == ["output_limit"] and closed == [1, 2]
    for fd, body, prefix_limit in ((1, stdout, _MIB), (2, stderr, 16 * 1024)):
        stream = capture.streams[fd].result()
        assert stream.data() == body[:prefix_limit]
        assert stream.captured_bytes == prefix_limit
        assert stream.observed_bytes == len(body)
        assert stream.observed_sha256 == hashlib.sha256(body).hexdigest()
        assert stream.truncated is True and stream.eof is False
        capture.pipe_data_received(fd, b"must-not-be-observed")
        assert capture.streams[fd].result() == stream


@pytest.mark.parametrize(
    "delivery_mode", [None, False, True], ids=["omitted", "default", "delivery"]
)
def test_windows_fingerprint_and_environment_are_purpose_bound_offline(
    simulated_windows: SimpleNamespace,
    delivery_mode: bool | None,
) -> None:
    fixture = simulated_windows
    options = {} if delivery_mode is None else {"for_delivery": delivery_mode}
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction, **options
    )
    for_delivery = delivery_mode is True
    assert driver.binding_fingerprint == _windows_fingerprint(fixture, for_delivery)
    assert driver._configuration == windows_reader._GitReadConfiguration(
        fixture.root,
        fixture.executable,
        fixture.state,
        fixture.capability,
        driver.binding_fingerprint,
        fixture.redaction,
        for_delivery,
    )
    assert windows_reader._environment(fixture.executable, **options) == _windows_environment(
        fixture.executable, for_delivery
    )
    assert driver.binding_fingerprint != _windows_fingerprint(fixture, not for_delivery)
    assert windows_reader.WINDOWS_GIT_ARGUMENTS == _WINDOWS_ARGUMENTS
    if for_delivery:
        legacy = _digest(
            {
                "native_binding": fixture.native.fingerprint,
                "capability": fixture.capability.digest,
                "executable": os.path.normcase(str(fixture.executable)),
                "state": os.path.normcase(str(fixture.state)),
                "purpose": "git-delivery-baseline/v1",
            }
        )
        assert driver.binding_fingerprint != legacy


@pytest.mark.parametrize("for_delivery", [False, True])
def test_windows_runtime_forwards_profile_and_private_state_offline(
    simulated_windows: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    for_delivery: bool,
) -> None:
    fixture = simulated_windows
    # 不修改全局os.name，不把宿主Path或Win32实现伪装成真实Windows。
    monkeypatch.setattr(git_reader, "os", SimpleNamespace(name="nt", devnull="NUL"))
    reader = git_reader.GitReadRuntime(
        fixture.root,
        fixture.executable,
        state_directory=fixture.state,
        output_redaction=fixture.redaction,
        for_delivery=for_delivery,
    )
    process = reader._runtime()
    assert isinstance(process, windows_reader.WindowsGitReadProcess)
    assert process._configuration.for_delivery is for_delivery
    assert process._configuration.state == fixture.state
    assert process._configuration.output_redaction is fixture.redaction
    assert reader._global_arguments == _arguments("nt", for_delivery)
    assert reader.contract() == {
        **_PUBLIC_CONTRACT,
        "implementation": "git-baseline-read/v1" if for_delivery else "git-read/windows-job-v1",
        "binding": (
            _baseline_binding(_windows_fingerprint(fixture, True))
            if for_delivery
            else _windows_fingerprint(fixture, False)
        ),
    }
    if for_delivery:
        assert reader.contract()["binding"] != process.binding_fingerprint
    ordinary = git_reader.GitReadRuntime(
        fixture.root, fixture.executable, state_directory=fixture.state
    )
    assert ordinary.contract()["binding"] == _windows_fingerprint(fixture, False)
    assert ordinary._global_arguments == _WINDOWS_ARGUMENTS


def _result(body: bytes) -> ProcessResult:
    def stream(data: bytes) -> ProcessStream:
        return ProcessStream(
            data_base64=base64.b64encode(data).decode("ascii"),
            captured_bytes=len(data),
            observed_bytes=len(data),
            observed_sha256=hashlib.sha256(data).hexdigest(),
            truncated=False,
            eof=True,
        )

    return ProcessResult(
        pid=12345,
        returncode=0,
        stop_reason="exited",
        termination="none",
        stdout=stream(body),
        stderr=stream(b""),
        elapsed_seconds=0.0,
    )


def _baseline_result(body: bytes) -> GitBaselineReadResult:
    return GitBaselineReadResult.from_posix_capture(_result(body))


def _signed_handle(fixture, bodies, raw_bodies, *, version=2, sequence=2, raw_eof=None):
    """只写合成安全前缀；保留真实Supervisor回执读取、验MAC和Lease/序号核验。"""
    directory = fixture.state / str(uuid4())
    directory.mkdir(mode=0o700, parents=True)
    now = datetime.now(UTC)

    def safe(body):
        digest = hashlib.sha256(body).hexdigest()
        return ProcessOutputObservation(
            observed_bytes=len(body),
            persisted_bytes=len(body),
            sha256=digest,
            persisted_sha256=digest,
            truncated=False,
            eof=True,
        )

    facts = dict(
        process_id=uuid4(),
        owner_identity="e" * 64,
        owner_token="d" * 64,
        state="exited",
        sequence=sequence,
        pid=12345,
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        returncode=0,
        stop_reason="exited",
        stdout=safe(bodies["stdout"]),
        stderr=safe(bodies["stderr"]),
    )
    if version == 2:
        facts.update(
            {
                "raw_" + name: RawProcessOutputObservation(
                    observed_bytes=len(body),
                    sha256=hashlib.sha256(body).hexdigest(),
                    eof=name != raw_eof,
                )
                for name, body in raw_bodies.items()
            }
        )
    receipt = sign_owner_receipt(**facts)
    lease = ProcessLease(
        process_id=receipt.process_id,
        plan_id=uuid4(),
        plan_fingerprint="a" * 64,
        process_spec_digest="b" * 64,
        capability_digest="c" * 64,
        launch_binding_digest="f" * 64,
        lifecycle="foreground",
        state="exited",
        sequence=7,
        owner_token="d" * 64,
        owner_identity=receipt.owner_identity,
        pid=receipt.pid,
        deadline=now + timedelta(seconds=5),
        started_at=receipt.started_at,
        finished_at=receipt.finished_at,
        returncode=receipt.returncode,
        stop_reason=receipt.stop_reason,
        stdout=receipt.stdout,
        stderr=receipt.stderr,
    )
    write_owner_receipt(directory / "receipt.json", receipt)
    for name, body in bodies.items():
        path = directory / (name + ".bin")
        path.write_bytes(body)
        path.chmod(0o600)
    handle = SupervisedProcess(Mock(), lease, directory, receipt.owner_identity)
    handle._last_receipt_sequence = sequence
    return handle, receipt, directory


def _install_owner(monkeypatch, handles):
    supervisor = AsyncMock()
    supervisor.__aenter__.return_value = supervisor
    supervisor.start.side_effect = handles
    factory = Mock(return_value=supervisor)
    monkeypatch.setattr(windows_reader, "WindowsProcessSupervisor", factory)
    plans = MagicMock()
    plans.__enter__.return_value = plans
    monkeypatch.setattr(windows_reader, "SQLiteExecutionPlanStore", Mock(return_value=plans))
    return factory, supervisor, plans


def _offline_query_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    for_delivery: bool,
) -> tuple[git_reader.GitReadRuntime, bytes]:
    """只模拟命令生成端口；NT输入必须是带盘符的真实Windows路径形状。"""
    monkeypatch.setattr(
        git_reader,
        "os",
        SimpleNamespace(name=platform, devnull="NUL" if platform == "nt" else "/dev/null"),
    )
    monkeypatch.setattr(
        git_reader,
        "_build_git_process",
        lambda *_args, **_kwargs: SimpleNamespace(binding_fingerprint="3" * 64),
    )
    if platform == "nt":
        expected = PureWindowsPath("C:/workspace/工程")
        root = Mock(spec=Path)
        root.absolute.return_value = expected
        root_line = (expected.as_posix() + "\r\n").encode()
    else:
        root = tmp_path
        root_line = (str(tmp_path) + "\n").encode()
    reader = git_reader.GitReadRuntime(root, tmp_path / "unused-git", for_delivery=for_delivery)
    return reader, root_line


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("for_delivery", [False, True])
@pytest.mark.parametrize("operation", ["status", "worktree", "staged"])
async def test_generated_queries_keep_all_fixed_guards_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    for_delivery: bool,
    operation: str,
) -> None:
    reader, root_line = _offline_query_runtime(tmp_path, monkeypatch, platform, for_delivery)
    run = AsyncMock(side_effect=[_result(root_line), _result(b""), _result(b"")])
    baseline = AsyncMock(side_effect=[_baseline_result(root_line), _baseline_result(b"")])
    if for_delivery:
        run = AsyncMock(return_value=_result(b""))
    monkeypatch.setattr(reader, "_run", run)
    monkeypatch.setattr(reader, "_run_baseline", baseline)
    cancel = CancelToken()
    args = _arguments(platform, for_delivery)
    if operation == "status":
        request = GitStatusInput()
        command = (
            "status",
            "--porcelain=v2",
            "--branch",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "-z",
        )
    else:
        request = GitDiffInput(target=operation, context_lines=0)
        command = (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--unified=0",
            *(("--cached",) if operation == "staged" else ()),
            "--",
        )
    await reader.execute(request, cancel)
    checks = [
        call((*args, "rev-parse", "--show-toplevel"), cancel, repository_check=True),
        call((*args, "config", "--no-includes", "--null", "--name-only", "--list"), cancel),
    ]
    if for_delivery:
        assert baseline.await_args_list == checks
        run.assert_awaited_once_with((*args, *command), cancel)
    else:
        assert run.await_args_list == [*checks, call((*args, *command), cancel)]
        baseline.assert_not_awaited()


@pytest.mark.parametrize("for_delivery", [False, True])
@pytest.mark.parametrize(
    "observed",
    [
        "/workspace/工程\n".encode(),
        "D:/workspace/工程\n".encode(),
        "C:/workspace/工程/child\n".encode(),
        "C:workspace/工程\n".encode(),
        b"C:/workspace\n",
        "C:/workspace/工程\n\r\n".encode(),
    ],
    ids=["drive-less", "other-drive", "child", "drive-relative", "parent", "extra-line"],
)
async def test_simulated_windows_queries_still_deny_wrong_root_before_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    for_delivery: bool,
    observed: bytes,
) -> None:
    """正确模拟路径不能豁免仓库根校验；拒绝后不运行配置或状态查询。"""
    reader, _ = _offline_query_runtime(tmp_path, monkeypatch, "nt", for_delivery)
    run = AsyncMock(
        return_value=(_baseline_result(observed) if for_delivery else _result(observed))
    )
    monkeypatch.setattr(reader, "_run_baseline" if for_delivery else "_run", run)
    cancel = CancelToken()
    with pytest.raises(git_reader.ReadToolError) as error:
        await reader.execute(GitStatusInput(), cancel)
    assert error.value.code == "path_denied"
    run.assert_awaited_once_with(
        (*_arguments("nt", for_delivery), "rev-parse", "--show-toplevel"),
        cancel,
        repository_check=True,
    )


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("metadata", ["root", "config"])
@pytest.mark.parametrize("changed_length", [False, True])
async def test_baseline_prechecks_never_parse_redacted_metadata_offline(
    tmp_path, monkeypatch, platform, metadata, changed_length
):
    reader, root_line = _offline_query_runtime(tmp_path, monkeypatch, platform, True)
    safe_body = root_line if metadata == "root" else b"core.safe\0"
    original = b"x" * (len(safe_body) + int(changed_length))
    changed = GitBaselineReadResult(
        _result(safe_body),
        RawProcessOutputObservation(
            observed_bytes=len(original), sha256=hashlib.sha256(original).hexdigest(), eof=True
        ),
        RawProcessOutputObservation(
            observed_bytes=0, sha256=hashlib.sha256(b"").hexdigest(), eof=True
        ),
    )
    baseline = AsyncMock(
        side_effect=([changed] if metadata == "root" else [_baseline_result(root_line), changed])
    )
    ordinary = AsyncMock(side_effect=AssertionError("前检失败不得运行Tool查询"))
    monkeypatch.setattr(reader, "_run_baseline", baseline)
    monkeypatch.setattr(reader, "_run", ordinary)
    with pytest.raises(KernelError) as denied:
        await reader.execute(GitStatusInput(), CancelToken())
    assert denied.value.code == "git_baseline_metadata_changed"
    assert baseline.await_count == (1 if metadata == "root" else 2)
    ordinary.assert_not_awaited()


@pytest.mark.parametrize(
    ("for_delivery", "with_redaction"),
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["default-raw", "default-redacted", "delivery-raw", "delivery-redacted"],
)
async def test_windows_execution_keeps_capture_limits_and_profile_spec_offline(
    simulated_windows: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    for_delivery: bool,
    with_redaction: bool,
) -> None:
    fixture = simulated_windows
    source = fixture.redaction if with_redaction else None
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root,
        fixture.executable,
        fixture.state,
        source,
        for_delivery=for_delivery,
    )
    bodies = {"stdout": b"a" * (_MIB + 7), "stderr": b"b" * (16 * 1024 + 7)}

    def observation(body: bytes) -> ProcessOutputObservation:
        digest = hashlib.sha256(body).hexdigest()
        return ProcessOutputObservation(
            observed_bytes=len(body),
            persisted_bytes=len(body),
            sha256=digest,
            persisted_sha256=digest,
            truncated=False,
            eof=True,
        )

    lease = SimpleNamespace(
        state="exited",
        stop_reason="exited",
        returncode=0,
        pid=12345,
        stdout=observation(bodies["stdout"]),
        stderr=observation(bodies["stderr"]),
    )
    handle = SimpleNamespace(
        wait=AsyncMock(return_value=lease),
        output=AsyncMock(side_effect=lambda name: bodies[name]),
    )
    supervisor = AsyncMock()
    supervisor.__aenter__.return_value = supervisor
    supervisor.start.return_value = handle
    supervisor_factory = Mock(return_value=supervisor)
    monkeypatch.setattr(windows_reader, "WindowsProcessSupervisor", supervisor_factory)
    plans = MagicMock()
    plans.__enter__.return_value = plans
    store_factory = Mock(return_value=plans)
    monkeypatch.setattr(windows_reader, "SQLiteExecutionPlanStore", store_factory)
    request = ProcessRequest(
        program="git", arguments=(*_arguments("nt", for_delivery), "status"), timeout_seconds=5.0
    )
    cancel = CancelToken()
    async with driver:
        result = await driver.run(request, cancel)
    assert driver._configuration.output_redaction is source
    supervisor_factory.assert_called_once_with(
        fixture.state / "process-owner", output_redaction=source
    )
    store_factory.assert_called_once_with(fixture.state / "execution-plans.db")
    plan, spec, capability = supervisor.start.await_args.args
    assert capability == fixture.capability
    assert spec.invocation == "argv" and spec.argv == (str(fixture.executable), *request.arguments)
    assert spec.timeout_seconds == 5.0 and spec.output_bytes == (9 if for_delivery else 8) * _MIB
    assert spec.terminal == "pipe" and spec.stdin == "closed" and spec.input_bytes == 0
    assert spec.lifecycle == "foreground" and spec.shell_source is None
    assert supervisor.start.await_args.kwargs == {
        "workspace": fixture.root,
        "environment": _windows_environment(fixture.executable, for_delivery),
    }
    assert plan.intent.tool_fingerprint == _windows_fingerprint(fixture, for_delivery)
    assert plan.sandbox.profile_digest == plan.intent.tool_fingerprint
    assert plan.intent.source == "builtin" and plan.intent.effect_class.value == "read_only"
    assert (
        plan.policy.version == "git-read/windows-v1" and plan.policy.policy_id == "git.read.fixed"
    )
    assert plan.policy.decision.value == "allow"
    plans.save_plan.assert_called_once_with(plan)
    handle.wait.assert_awaited_once()
    handle.output.assert_has_awaits([call("stdout"), call("stderr")])
    fixture.native.verify.assert_called_once_with()
    for name, limit in (("stdout", _MIB), ("stderr", 16 * 1024)):
        stream = getattr(result, name)
        assert stream.data() == bodies[name][:limit] and stream.captured_bytes == limit
        assert stream.observed_bytes == len(bodies[name])
        assert stream.observed_sha256 == hashlib.sha256(bodies[name]).hexdigest()
        assert stream.eof is True and stream.truncated is True


async def test_windows_baseline_requires_delivery_profile_before_owner_offline(
    simulated_windows, monkeypatch
):
    fixture = simulated_windows
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction
    )
    execute = AsyncMock(side_effect=AssertionError("普通端口不得签发基准结果"))
    monkeypatch.setattr(windows_reader, "_execute_git", execute)
    with pytest.raises(KernelError) as denied:
        await driver.run_baseline(ProcessRequest(program="git", timeout_seconds=5.0), CancelToken())
    assert denied.value.code == "git_baseline_reader_required"
    execute.assert_not_awaited()


@pytest.mark.parametrize("source_truthy", [True, False], ids=["truthy-source", "falsy-source"])
@pytest.mark.parametrize("entrypoint", ["process", "runtime"])
async def test_windows_raw_baseline_uses_authenticated_v2_not_redacted_digest_offline(
    simulated_windows, monkeypatch, source_truthy, entrypoint
):
    fixture = simulated_windows
    source = MagicMock()
    source.__bool__.return_value = source_truthy
    bodies = {"stdout": b"[REDACTED]", "stderr": b"safe diagnostic"}
    original = {"stdout": b"synthetic-raw-blob", "stderr": b"original-diagnostic"}
    handle, receipt, _ = _signed_handle(fixture, bodies, original)
    authenticate = AsyncMock(wraps=handle._terminal_owner_receipt)
    monkeypatch.setattr(handle, "_terminal_owner_receipt", authenticate)
    factory, supervisor, plans = _install_owner(monkeypatch, [handle])
    arguments = (*_arguments("nt", True), "cat-file", "blob", "a" * 40)
    if entrypoint == "process":
        driver = windows_reader.WindowsGitReadProcess(
            fixture.root, fixture.executable, fixture.state, source, for_delivery=True
        )
        observed = await driver.run_baseline(
            ProcessRequest(program="git", arguments=arguments, timeout_seconds=5.0), CancelToken()
        )
    else:
        monkeypatch.setattr(git_reader, "os", SimpleNamespace(name="nt", devnull="NUL"))
        reader = git_reader.GitReadRuntime(
            fixture.root,
            fixture.executable,
            state_directory=fixture.state,
            output_redaction=source,
            for_delivery=True,
        )
        observed = await reader._run_baseline(arguments, CancelToken())
    authenticate.assert_awaited_once_with()
    factory.assert_called_once_with(fixture.state / "process-owner", output_redaction=source)
    plan, spec, _ = supervisor.start.await_args.args
    assert spec.argv == (str(fixture.executable), *arguments)
    assert spec.timeout_seconds == 5.0 and spec.output_bytes == 9 * _MIB
    assert spec.terminal == "pipe" and spec.stdin == "closed"
    assert supervisor.start.await_args.kwargs["environment"] == _windows_environment(
        fixture.executable, True
    )
    plans.save_plan.assert_called_once_with(plan)
    assert isinstance(observed, GitBaselineReadResult)
    assert observed.result.stdout.data() == bodies["stdout"]
    assert observed.result.stdout.observed_sha256 == hashlib.sha256(bodies["stdout"]).hexdigest()
    assert observed.raw_stdout == receipt.raw_stdout
    assert observed.raw_stderr == receipt.raw_stderr
    assert observed.raw_stdout.sha256 != observed.result.stdout.observed_sha256
    assert observed.raw_stdout.observed_bytes != observed.result.stdout.observed_bytes
    fixture.native.verify.assert_called_once_with()


@pytest.mark.parametrize("with_redaction", [False, True])
async def test_windows_old_v1_cannot_prove_raw_even_without_redaction_offline(
    simulated_windows, monkeypatch, with_redaction
):
    fixture = simulated_windows
    bodies = {"stdout": b"unchanged", "stderr": b""}
    handle, _, _ = _signed_handle(fixture, bodies, bodies, version=1)
    _install_owner(monkeypatch, [handle])
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root,
        fixture.executable,
        fixture.state,
        fixture.redaction if with_redaction else None,
        for_delivery=True,
    )
    with pytest.raises(KernelError) as denied:
        await driver.run_baseline(ProcessRequest(program="git", timeout_seconds=5.0), CancelToken())
    assert denied.value.code == "git_baseline_raw_observation_required"


@pytest.mark.parametrize(
    "failure", ["raw-mac", "receipt-sequence", "raw-stdout-eof", "raw-stderr-eof"]
)
async def test_windows_baseline_reverifies_raw_and_terminal_sequence_offline(
    simulated_windows, monkeypatch, failure
):
    fixture = simulated_windows
    bodies = {"stdout": b"safe", "stderr": b""}
    handle, receipt, directory = _signed_handle(
        fixture,
        bodies,
        bodies,
        raw_eof=(
            failure.removeprefix("raw-").removesuffix("-eof") if failure.endswith("-eof") else None
        ),
    )
    if failure == "raw-mac":
        changed = receipt.model_copy(
            update={"raw_stdout": receipt.raw_stdout.model_copy(update={"sha256": "0" * 64})}
        )
        write_owner_receipt(directory / "receipt.json", changed)
    elif failure == "receipt-sequence":
        handle._last_receipt_sequence = 1
    _install_owner(monkeypatch, [handle])
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction, for_delivery=True
    )
    error_type = git_reader.ReadToolError if failure.endswith("-eof") else KernelError
    with pytest.raises(error_type) as denied:
        await driver.run_baseline(ProcessRequest(program="git", timeout_seconds=5.0), CancelToken())
    assert denied.value.code == (
        "io_failed" if failure.endswith("-eof") else "process_owner_receipt_invalid"
    )


@pytest.mark.parametrize(
    "failure", ["cancelled", "timeout", "output_limit", "unknown", "rc", "stdout", "stderr"]
)
async def test_windows_baseline_never_authenticates_failed_or_incomplete_exit_offline(
    simulated_windows, monkeypatch, failure
):
    fixture = simulated_windows
    bodies = {"stdout": b"safe", "stderr": b""}
    actual, _, _ = _signed_handle(fixture, bodies, bodies)
    changes = {}
    if failure == "unknown":
        changes.update(state="unknown", stop_reason="unknown")
    elif failure == "rc":
        changes["returncode"] = 1
    elif failure in {"stdout", "stderr"}:
        changes[failure] = getattr(actual.lease, failure).model_copy(update={"eof": False})
    else:
        changes["stop_reason"] = failure
    handle = SimpleNamespace(
        wait=AsyncMock(return_value=actual.lease.model_copy(update=changes)),
        output=AsyncMock(side_effect=lambda name: bodies[name]),
        _terminal_owner_receipt=AsyncMock(side_effect=AssertionError("失败退出不得取raw证明")),
    )
    _install_owner(monkeypatch, [handle])
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction, for_delivery=True
    )
    error_type = TurnCancelled if failure == "cancelled" else git_reader.ReadToolError
    with pytest.raises(error_type) as denied:
        await driver.run_baseline(ProcessRequest(program="git", timeout_seconds=5.0), CancelToken())
    if failure != "cancelled":
        assert denied.value.code == (
            "timeout" if failure == "timeout" else "not_found" if failure == "rc" else "io_failed"
        )
    handle._terminal_owner_receipt.assert_not_awaited()


async def test_concurrent_windows_reads_keep_each_authenticated_observation_offline(
    simulated_windows, monkeypatch
):
    fixture = simulated_windows
    first_safe = {"stdout": b"safe-one", "stderr": b""}
    second_safe = {"stdout": b"safe-two", "stderr": b""}
    first_raw = {"stdout": b"synthetic-original-one", "stderr": b""}
    second_raw = {"stdout": b"synthetic-original-two-is-longer", "stderr": b""}
    first, _, _ = _signed_handle(fixture, first_safe, first_raw)
    second, _, _ = _signed_handle(fixture, second_safe, second_raw)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_wait(_cancel):
        entered.set()
        await release.wait()
        return first.lease

    monkeypatch.setattr(first, "wait", delayed_wait)
    _install_owner(monkeypatch, [first, second])
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction, for_delivery=True
    )
    request = ProcessRequest(program="git", timeout_seconds=5.0)
    pending = asyncio.create_task(driver.run_baseline(request, CancelToken()))
    await asyncio.wait_for(entered.wait(), 2)
    observed_second = await driver.run_baseline(request, CancelToken())
    release.set()
    observed_first = await pending
    for observed, safe, original in (
        (observed_first, first_safe, first_raw),
        (observed_second, second_safe, second_raw),
    ):
        assert observed.result.stdout.data() == safe["stdout"]
        assert observed.raw_stdout.sha256 == hashlib.sha256(original["stdout"]).hexdigest()


@pytest.mark.parametrize("failure", ["token", "task"])
@pytest.mark.parametrize("baseline", [False, True])
async def test_windows_shared_drive_cancels_and_drains_before_returning_offline(
    simulated_windows, monkeypatch, failure, baseline
):
    fixture = simulated_windows
    entered, drained = asyncio.Event(), asyncio.Event()

    async def blocked(_config, _request, operation, *, baseline):
        entered.set()
        await operation._event.wait()
        drained.set()
        raise TurnCancelled

    monkeypatch.setattr(windows_reader, "_execute_git", blocked)
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, fixture.redaction, for_delivery=True
    )
    cancel = CancelToken()
    entrypoint = driver.run_baseline if baseline else driver.run
    pending = asyncio.create_task(
        entrypoint(ProcessRequest(program="git", timeout_seconds=5.0), cancel)
    )
    await asyncio.wait_for(entered.wait(), 2)
    if failure == "token":
        cancel.cancel()
    else:
        pending.cancel()
    with pytest.raises(TurnCancelled if failure == "token" else asyncio.CancelledError):
        await pending
    assert drained.is_set() and pending.done()


@pytest.mark.parametrize("for_delivery", [False, True])
@pytest.mark.parametrize("drift", ["purpose", "native_binding"])
async def test_windows_revalidates_purpose_and_native_binding_before_owner_offline(
    simulated_windows: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    for_delivery: bool,
    drift: str,
) -> None:
    fixture = simulated_windows
    driver = windows_reader.WindowsGitReadProcess(
        fixture.root, fixture.executable, fixture.state, for_delivery=for_delivery
    )
    config = driver._configuration
    if drift == "purpose":
        config = replace(config, for_delivery=not for_delivery)
    else:
        fixture.native.fingerprint = "4" * 64
    supervisor_factory = Mock(side_effect=AssertionError("绑定漂移不得进入Owner"))
    monkeypatch.setattr(windows_reader, "WindowsProcessSupervisor", supervisor_factory)
    with pytest.raises(KernelError) as changed:
        await windows_reader._execute_git(
            config, ProcessRequest(program="git", timeout_seconds=5.0), CancelToken()
        )
    assert changed.value.code == "process_binding_changed"
    supervisor_factory.assert_not_called()


@pytest.mark.parametrize("for_delivery", [False, True])
@pytest.mark.parametrize("state_case", ["missing", "relative", "child", "parent"])
def test_windows_delivery_does_not_bypass_private_state_guards_offline(
    simulated_windows: SimpleNamespace,
    for_delivery: bool,
    state_case: str,
) -> None:
    fixture = simulated_windows
    states = {
        "missing": None,
        "relative": Path("relative-state"),
        "child": fixture.root / "state",
        "parent": fixture.root.parent,
    }
    with pytest.raises(KernelError) as denied:
        windows_reader.WindowsGitReadProcess(
            fixture.root, fixture.executable, states[state_case], for_delivery=for_delivery
        )
    assert denied.value.code == (
        "product_git_state_required"
        if state_case in {"missing", "relative"}
        else "product_state_overlap"
    )
