from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionPlanV2,
    canonical_digest,
    execution_is_approved,
)
from harnessix.execution.planner import bind_environment
from harnessix.processes.owner_protocol import ProcessOwnerCommand, ProcessOwnerStart
from harnessix.processes.owner_receipt import ProcessOwnerReceipt, read_owner_receipt
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessLease,
    ProcessSpec,
)
from harnessix.processes.supervision_planner import build_process_capability, prepare_process_lease
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.processes.windows_conpty import conpty_available
from harnessix.secrets.provider import ResolvedSecretEnvironment
from harnessix.workspace.snapshot import verify_workspace_snapshot

_TERMINAL_STATES = frozenset({"exited", "failed", "unknown"})
_WINDOWS_SPAWN_LOCK = threading.Lock()


def posix_process_implementation_digest() -> str:
    root = Path(__file__).parent
    paths = tuple(
        root / name
        for name in (
            "owner_protocol.py",
            "owner_output.py",
            "owner_receipt.py",
            "posix_owner.py",
            "supervision_contracts.py",
            "supervisor.py",
        )
    )
    try:
        executable = Path(sys.executable).resolve(strict=True)
        executable_info = executable.stat()
        payload = {
            "implementation": "harnessix.posix-process-owner/v1",
            "python": {
                "path": str(executable),
                "device": executable_info.st_dev,
                "inode": executable_info.st_ino,
                "size": executable_info.st_size,
                "mtime_ns": executable_info.st_mtime_ns,
            },
            "modules": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        }
    except OSError:
        raise KernelError("process_capability_unavailable", "POSIX Process实现不可证明") from None
    return canonical_digest(payload)


def probe_posix_process_capability() -> ProcessCapabilityProbe:
    if os.name != "posix":
        raise KernelError("process_platform_unsupported", "POSIX Process能力不可用")
    return build_process_capability(
        platform="posix",
        supports_pty=True,
        implementation_digest=posix_process_implementation_digest(),
    )


def windows_process_implementation_digest() -> str:
    if os.name != "nt":
        raise KernelError("process_platform_unsupported", "Windows Process能力不可用")
    root = Path(__file__).parent
    paths = tuple(
        root / name
        for name in (
            "owner_output.py",
            "owner_protocol.py",
            "owner_receipt.py",
            "supervision_contracts.py",
            "supervisor.py",
            "windows_job.py",
            "windows_conpty.py",
            "windows_owner.py",
        )
    )
    executables = (
        Path(sys.executable),
        Path(shutil.which("cmd.exe") or ""),
        Path(shutil.which("powershell.exe") or ""),
    )
    try:
        if any(not path.is_absolute() for path in executables):
            raise OSError
        payload = {
            "implementation": "harnessix.windows-process-owner/v1",
            "executables": {
                str(path.resolve(strict=True)): {
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                }
                for path in executables
            },
            "modules": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        }
    except OSError:
        raise KernelError("process_capability_unavailable", "Windows Process实现不可证明") from None
    return canonical_digest(payload)


def probe_windows_process_capability() -> ProcessCapabilityProbe:
    return build_process_capability(
        platform="windows",
        supports_pty=conpty_available(),
        implementation_digest=windows_process_implementation_digest(),
    )


def _validated_lease(lease: ProcessLease, **changes: object) -> ProcessLease:
    candidate = lease.model_copy(update=changes)
    try:
        return ProcessLease.model_validate_json(candidate.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_lease_invalid", "Process Lease状态事实无效") from None


def _safe_state_root(value: str | Path) -> Path:
    path = Path(value)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError
        if os.name == "posix":
            path.chmod(0o700)
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
    except (OSError, ValueError):
        raise KernelError("process_state_invalid", "Process状态目录无效") from None
    return path


def _materialize_argv(spec: ProcessSpec) -> tuple[str, ...]:
    if spec.invocation == "argv":
        return spec.argv
    if spec.invocation == "posix_sh":
        assert spec.shell_source is not None
        return ("/bin/sh", "-c", spec.shell_source)
    raise KernelError("process_capability_mismatch", "当前POSIX owner不支持该调用模式")


class SupervisedProcess:
    def __init__(
        self,
        store: SQLiteProcessLeaseStore,
        lease: ProcessLease,
        run_directory: Path,
        owner_identity: str | None,
        *,
        control_fd: int | None = None,
        owner: subprocess.Popen[bytes] | None = None,
    ) -> None:
        self._store = store
        self._lease = lease
        self._run_directory = run_directory
        self._owner_identity = owner_identity
        self._control_fd = control_fd
        self._owner = owner
        self._lock = asyncio.Lock()
        self._last_receipt_sequence = 0
        self._closed = False

    @property
    def lease(self) -> ProcessLease:
        return self._lease

    async def refresh(self) -> ProcessLease:
        async with self._lock:
            if self._lease.state in _TERMINAL_STATES:
                return self._lease
            try:
                receipt = await asyncio.to_thread(
                    read_owner_receipt,
                    self._run_directory / "receipt.json",
                    owner_token=self._lease.owner_token,
                    process_id=self._lease.process_id,
                    owner_identity=self._owner_identity,
                )
            except KernelError as error:
                if error.code == "process_owner_receipt_missing" and not self._owner_exited():
                    return self._lease
                if error.code == "process_owner_receipt_missing":
                    return self._mark_unknown("cleanup_failed")
                raise
            if self._owner_identity is None:
                self._owner_identity = receipt.owner_identity
            if receipt.sequence <= self._last_receipt_sequence:
                return self._lease
            self._last_receipt_sequence = receipt.sequence
            return self._apply_receipt(receipt)

    async def wait(self, cancel: CancelToken | None = None) -> ProcessLease:
        cancellation_sent = False
        try:
            while self._lease.state not in _TERMINAL_STATES:
                if cancel is not None and cancel.cancelled and not cancellation_sent:
                    cancellation_sent = True
                    await self.stop("cancelled")
                await self.refresh()
                if self._lease.state not in _TERMINAL_STATES:
                    await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            await self.stop("cancelled")
            await asyncio.shield(self._wait_terminal())
            raise
        await self._reap_owner()
        return self._lease

    async def send_stdin(self, data: bytes) -> None:
        if type(data) is not bytes or not data or len(data) > 64 * 1024:
            raise KernelError("process_input_invalid", "Process stdin分片无效")
        await self._send(
            ProcessOwnerCommand(
                operation="stdin", data_base64=base64.b64encode(data).decode("ascii")
            )
        )

    async def close_stdin(self) -> None:
        await self._send(ProcessOwnerCommand(operation="close_stdin"))

    async def resize(self, columns: int, rows: int) -> None:
        try:
            command = ProcessOwnerCommand(operation="resize", columns=columns, rows=rows)
        except ValidationError:
            raise KernelError("process_terminal_invalid", "Process终端尺寸无效") from None
        await self._send(command)

    async def stop(self, reason: Literal["cancelled", "closed"] = "cancelled") -> None:
        async with self._lock:
            if self._lease.state in _TERMINAL_STATES:
                return
            if self._lease.state == "running":
                updated = _validated_lease(
                    self._lease,
                    state="stopping",
                    sequence=self._lease.sequence + 1,
                )
                self._store.transition(self._lease, updated)
                self._lease = updated
            try:
                await self._send_locked(ProcessOwnerCommand(operation="stop", reason=reason))
            except KernelError as error:
                if error.code != "process_control_lost":
                    raise

    async def output(self, stream: Literal["stdout", "stderr"]) -> bytes:
        await self.refresh()
        observation = self._lease.stdout if stream == "stdout" else self._lease.stderr
        path = self._run_directory / f"{stream}.bin"
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                body = bytearray()
                while len(body) < observation.persisted_bytes:
                    chunk = os.read(descriptor, observation.persisted_bytes - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
            finally:
                os.close(descriptor)
        except OSError:
            raise KernelError("process_output_corrupt", "Process输出Artifact不可读取") from None
        result = bytes(body)
        if (
            len(result) != observation.persisted_bytes
            or hashlib.sha256(result).hexdigest() != observation.persisted_sha256
        ):
            raise KernelError("process_output_corrupt", "Process输出Artifact与Lease不一致")
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lease.state not in _TERMINAL_STATES:
            await self.stop("closed")
            await self.wait()
        self._close_control()

    async def _send(self, command: ProcessOwnerCommand) -> None:
        async with self._lock:
            await self._send_locked(command)

    async def _send_locked(self, command: ProcessOwnerCommand) -> None:
        if self._lease.state in _TERMINAL_STATES or self._control_fd is None:
            raise KernelError("process_not_owned", "当前Runtime不拥有该Process控制通道")
        body = command.model_dump_json(warnings="error").encode("utf-8") + b"\n"
        try:
            await asyncio.to_thread(self._write_all, self._control_fd, body)
        except OSError:
            self._close_control()
            raise KernelError("process_control_lost", "Process owner控制通道已经断开") from None

    @staticmethod
    def _write_all(descriptor: int, body: bytes) -> None:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short control write")
            view = view[written:]

    def _apply_receipt(self, receipt: ProcessOwnerReceipt) -> ProcessLease:
        current = self._lease
        if receipt.state == "running":
            state = "stopping" if current.state == "stopping" else "running"
            changes: dict[str, object] = {
                "state": state,
                "sequence": current.sequence + 1,
                "owner_identity": receipt.owner_identity,
                "pid": receipt.pid,
                "started_at": receipt.started_at,
                "stdout": receipt.stdout,
                "stderr": receipt.stderr,
            }
        elif receipt.state == "failed":
            changes = {
                "state": "failed",
                "sequence": current.sequence + 1,
                "finished_at": receipt.finished_at,
                "stop_reason": "launch_failed",
                "stdout": receipt.stdout,
                "stderr": receipt.stderr,
            }
        elif receipt.state == "unknown":
            changes = {
                "state": "unknown",
                "sequence": current.sequence + 1,
                "owner_identity": receipt.owner_identity if receipt.pid is not None else None,
                "pid": receipt.pid,
                "started_at": receipt.started_at,
                "finished_at": receipt.finished_at,
                "stop_reason": receipt.stop_reason,
                "stdout": receipt.stdout,
                "stderr": receipt.stderr,
            }
        else:
            changes = {
                "state": "exited",
                "sequence": current.sequence + 1,
                "owner_identity": receipt.owner_identity,
                "pid": receipt.pid,
                "started_at": receipt.started_at,
                "finished_at": receipt.finished_at,
                "returncode": receipt.returncode,
                "stop_reason": receipt.stop_reason,
                "stdout": receipt.stdout,
                "stderr": receipt.stderr,
            }
        updated = _validated_lease(current, **changes)
        self._store.transition(current, updated)
        self._lease = updated
        if updated.state in _TERMINAL_STATES:
            self._close_control()
        return updated

    def _mark_unknown(self, reason: Literal["host_lost", "cleanup_failed"]) -> ProcessLease:
        current = self._lease
        updated = _validated_lease(
            current,
            state="unknown",
            sequence=current.sequence + 1,
            finished_at=datetime.now(UTC),
            stop_reason=reason,
        )
        self._store.transition(current, updated)
        self._lease = updated
        self._close_control()
        return updated

    def _owner_exited(self) -> bool:
        return self._owner is not None and self._owner.poll() is not None

    async def _wait_terminal(self) -> None:
        while self._lease.state not in _TERMINAL_STATES:
            await self.refresh()
            await asyncio.sleep(0.02)

    async def _reap_owner(self) -> None:
        if self._owner is not None:
            await asyncio.to_thread(self._owner.wait)
            self._owner = None

    def _close_control(self) -> None:
        if self._control_fd is not None:
            try:
                os.close(self._control_fd)
            except OSError:
                pass
            self._control_fd = None


class PosixProcessSupervisor:
    def __init__(self, state_root: str | Path, *, terminate_grace_seconds: float = 0.5) -> None:
        if os.name != "posix":
            raise KernelError("process_platform_unsupported", "POSIX Process Supervisor不可用")
        if not 0 <= terminate_grace_seconds <= 5:
            raise KernelError("process_invalid_limits", "Process终止宽限无效")
        self._root = _safe_state_root(state_root)
        self._runs = _safe_state_root(self._root / "runs")
        self._store = SQLiteProcessLeaseStore(self._root / "process-leases.db")
        self._terminate_grace = terminate_grace_seconds
        self._platform: Literal["posix", "windows"] = "posix"
        self._capability = probe_posix_process_capability()
        self._handles: dict[UUID, SupervisedProcess] = {}
        self._closed = False

    @property
    def capability(self) -> ProcessCapabilityProbe:
        return self._capability

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(
        self,
        plan: ExecutionPlanV2,
        spec: ProcessSpec,
        capability: ProcessCapabilityProbe,
        *,
        workspace: str | Path,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        checkpoint: ExecutionApprovalCheckpoint | None = None,
    ) -> SupervisedProcess:
        if self._closed:
            raise KernelError("process_closed", "Process Supervisor已经关闭")
        if not execution_is_approved(plan, checkpoint):
            raise KernelError("approval_required", "Execution Plan尚未获得有效批准")
        if capability != self._capability or self._probe_capability() != self._capability:
            raise KernelError("process_capability_mismatch", "Process owner能力与平台不一致")
        try:
            existing = self._store.load(spec.process_id)
        except KernelError as error:
            if error.code != "process_lease_not_found":
                raise
        else:
            if (
                existing.plan_id != plan.plan_id
                or existing.plan_fingerprint != plan.fingerprint
                or existing.process_spec_digest != spec.digest
                or existing.capability_digest != capability.digest
            ):
                raise KernelError("process_lease_conflict", "Process ID已绑定其他执行计划")
            raise KernelError("process_already_exists", "Process已经创建；禁止自动重放")
        verify_workspace_snapshot(plan.workspace, workspace)
        checked_environment = dict(environment)
        if bind_environment(checked_environment, platform=self._platform) != plan.environment:
            raise KernelError("execution_plan_stale", "Process环境与Execution Plan不一致")
        secret_values = {} if secrets is None else secrets.as_text()
        actual_bindings = () if secrets is None else secrets.bindings()
        expected_bindings = tuple(
            sorted((binding.target, binding.name, binding.version) for binding in plan.secrets)
        )
        if actual_bindings != expected_bindings or set(checked_environment) & set(secret_values):
            raise KernelError("secret_binding_mismatch", "Secret注入与Execution Plan不一致")
        checked_environment.update(secret_values)
        lease = prepare_process_lease(plan, spec, capability)
        owner_identity = os.urandom(32).hex()
        try:
            request = ProcessOwnerStart(
                process_id=spec.process_id,
                owner_identity=owner_identity,
                owner_token=lease.owner_token,
                argv=self._materialize_argv(spec),
                cwd=str(
                    Path(workspace) / ("" if plan.workspace.cwd == "." else plan.workspace.cwd)
                ),
                environment=checked_environment,
                secret_names=tuple(sorted(secret_values)),
                terminal=spec.terminal,
                stdin=spec.stdin,
                deadline=lease.deadline,
                output_bytes=spec.output_bytes,
                input_bytes=spec.input_bytes,
                columns=spec.columns,
                rows=spec.rows,
                terminate_grace_seconds=self._terminate_grace,
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("process_spec_invalid", "Process owner启动请求无效") from None
        self._store.create(lease)
        run_directory = self._runs / str(spec.process_id)
        try:
            run_directory.mkdir(mode=0o700)
        except OSError:
            failed = _validated_lease(
                lease,
                state="failed",
                sequence=1,
                finished_at=datetime.now(UTC),
                stop_reason="launch_failed",
            )
            self._store.transition(lease, failed)
            raise KernelError("process_launch_failed", "Process owner状态目录创建失败") from None
        starting = _validated_lease(lease, state="starting", sequence=1)
        self._store.transition(lease, starting)
        read_fd, write_fd = os.pipe()
        owner: subprocess.Popen[bytes] | None = None
        try:
            owner = await asyncio.to_thread(self._spawn_owner, read_fd, run_directory)
            os.close(read_fd)
            read_fd = -1
            body = request.model_dump_json(warnings="error").encode("utf-8") + b"\n"
            await asyncio.to_thread(SupervisedProcess._write_all, write_fd, body)
        except (OSError, ValueError, subprocess.SubprocessError):
            if owner is not None:
                owner.kill()
                await asyncio.to_thread(owner.wait)
            for descriptor in (read_fd, write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            failed = _validated_lease(
                starting,
                state="failed",
                sequence=2,
                finished_at=datetime.now(UTC),
                stop_reason="launch_failed",
            )
            self._store.transition(starting, failed)
            raise KernelError("process_launch_failed", "Process owner启动失败") from None
        assert owner is not None
        handle = SupervisedProcess(
            self._store,
            starting,
            run_directory,
            owner_identity,
            control_fd=write_fd,
            owner=owner,
        )
        self._handles[spec.process_id] = handle
        for _ in range(500):
            await handle.refresh()
            if handle.lease.state != "starting":
                return handle
            await asyncio.sleep(0.01)
        await handle.stop("closed")
        await handle.wait()
        raise KernelError("process_launch_failed", "Process owner未在时限内报告启动结果")

    def _probe_capability(self) -> ProcessCapabilityProbe:
        return probe_posix_process_capability()

    def _materialize_argv(self, spec: ProcessSpec) -> tuple[str, ...]:
        return _materialize_argv(spec)

    def _spawn_owner(self, read_fd: int, run_directory: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            (
                sys.executable,
                "-m",
                "harnessix.processes.posix_owner",
                "--control-fd",
                str(read_fd),
                "--run-directory",
                str(run_directory),
            ),
            cwd=self._root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(read_fd,),
            start_new_session=True,
        )

    async def run(
        self,
        plan: ExecutionPlanV2,
        spec: ProcessSpec,
        capability: ProcessCapabilityProbe,
        *,
        workspace: str | Path,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        checkpoint: ExecutionApprovalCheckpoint | None = None,
        cancel: CancelToken | None = None,
    ) -> ProcessLease:
        handle = await self.start(
            plan,
            spec,
            capability,
            workspace=workspace,
            environment=environment,
            secrets=secrets,
            checkpoint=checkpoint,
        )
        return await handle.wait(cancel)

    async def reconcile(self, process_id: UUID) -> ProcessLease:
        lease = self._store.load(process_id)
        if lease.state in _TERMINAL_STATES:
            return lease
        handle = SupervisedProcess(
            self._store,
            lease,
            self._runs / str(process_id),
            lease.owner_identity,
        )
        if lease.state == "prepared":
            failed = _validated_lease(
                lease,
                state="failed",
                sequence=lease.sequence + 1,
                finished_at=datetime.now(UTC),
                stop_reason="launch_failed",
            )
            self._store.transition(lease, failed)
            return failed
        for _ in range(max(50, int((self._terminate_grace + 1) / 0.02))):
            await handle.refresh()
            if handle.lease.state in _TERMINAL_STATES:
                return handle.lease
            await asyncio.sleep(0.02)
        return handle._mark_unknown("host_lost")  # noqa: SLF001 - 恢复端无PID控制权限

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        handles = tuple(self._handles.values())
        if handles:
            await asyncio.gather(*(handle.aclose() for handle in handles))
        self._handles.clear()
        self._store.close()


class WindowsProcessSupervisor(PosixProcessSupervisor):
    def __init__(self, state_root: str | Path, *, terminate_grace_seconds: float = 0.5) -> None:
        if os.name != "nt":
            raise KernelError("process_platform_unsupported", "Windows Process Supervisor不可用")
        if not 0 <= terminate_grace_seconds <= 5:
            raise KernelError("process_invalid_limits", "Process终止宽限无效")
        self._root = _safe_state_root(state_root)
        self._runs = _safe_state_root(self._root / "runs")
        self._store = SQLiteProcessLeaseStore(self._root / "process-leases.db")
        self._terminate_grace = terminate_grace_seconds
        self._platform = "windows"
        self._capability = probe_windows_process_capability()
        self._handles = {}
        self._closed = False

    def _probe_capability(self) -> ProcessCapabilityProbe:
        return probe_windows_process_capability()

    def _materialize_argv(self, spec: ProcessSpec) -> tuple[str, ...]:
        if spec.invocation == "argv":
            argv = spec.argv
        else:
            assert spec.shell_source is not None
            source = spec.shell_source
            argv = ()
        if spec.invocation == "cmd":
            command = shutil.which("cmd.exe")
            if command is not None:
                argv = (command, "/d", "/s", "/c", source)
        elif spec.invocation == "powershell":
            powershell = shutil.which("powershell.exe")
            if powershell is not None:
                argv = (
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    source,
                )
        if not argv:
            raise KernelError("process_capability_unavailable", "Windows Shell实现不可用")
        if len(subprocess.list2cmdline(argv)) > 32766:
            raise KernelError("process_spec_invalid", "Windows命令行超过CreateProcessW上限")
        return argv

    def _spawn_owner(self, read_fd: int, run_directory: Path) -> subprocess.Popen[bytes]:
        import msvcrt

        handle = msvcrt.get_osfhandle(read_fd)  # type: ignore[attr-defined]
        startup = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startup.lpAttributeList = {"handle_list": [handle]}
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        owner_environment = {
            "PATH": os.pathsep.join(
                (
                    str(Path(sys.executable).parent),
                    str(Path(system_root) / "System32"),
                    system_root,
                )
            ),
            "SystemRoot": system_root,
            "WINDIR": system_root,
        }
        with _WINDOWS_SPAWN_LOCK:
            os.set_handle_inheritable(handle, True)  # type: ignore[attr-defined]
            try:
                return subprocess.Popen(
                    (
                        sys.executable,
                        "-m",
                        "harnessix.processes.windows_owner",
                        "--control-handle",
                        str(handle),
                        "--run-directory",
                        str(run_directory),
                    ),
                    cwd=self._root,
                    env=owner_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    startupinfo=startup,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
                )
            finally:
                os.set_handle_inheritable(handle, False)  # type: ignore[attr-defined]
