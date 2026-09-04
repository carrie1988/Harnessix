"""仅限受信宿主的POSIX执行层；不提供模型授权或跨重启PID操作。"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.processes.capture import CaptureProtocol
from harnessix.processes.contracts import ProcessLimits, ProcessRequest, ProcessResult, StopReason
from harnessix.tools.runtime import _drain

_ENV_NAMES = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "NO_COLOR"})
_DEFAULT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def _identity(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


@dataclass(slots=True)
class _Operation:
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    reason: StopReason | None = None

    def stop(self, reason: StopReason) -> None:
        if self.reason is None:
            self.reason = reason
            self.stopped.set()


class HostProcessRuntime:
    def __init__(
        self,
        cwd: str | Path,
        programs: Mapping[str, str | Path],
        *,
        environment: Mapping[str, str] | None = None,
        limits: ProcessLimits | None = None,
    ) -> None:
        if os.name != "posix":
            raise KernelError("process_platform_unsupported", "宿主进程当前仅支持POSIX")
        try:
            self._limits = ProcessLimits.model_validate_json(
                (limits or ProcessLimits()).model_dump_json(warnings="error")
            )
        except ValueError:
            raise KernelError("process_invalid_limits", "进程资源策略不符合契约") from None
        self._environment = dict(_DEFAULT_ENV if environment is None else environment)
        try:
            if (
                not self._environment.keys() <= _ENV_NAMES
                or any(type(v) is not str or "\0" in v for v in self._environment.values())
                or sum(len(k.encode()) + len(v.encode()) + 2 for k, v in self._environment.items())
                > 8192
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise KernelError(
                "process_environment_denied", "进程环境不符合宿主允许列表或大小限制"
            ) from None
        try:
            if not Path(cwd).is_absolute() or not 1 <= len(programs) <= 32:
                raise ValueError
            self._cwd = Path(cwd).resolve(strict=True)
            if not self._cwd.is_dir():
                raise ValueError
            self._cwd_identity = _identity(self._cwd)[:2]
            self._programs: dict[str, tuple[Path, tuple[int, ...]]] = {}
            for name, value in programs.items():
                ProcessRequest(program=name)
                if not Path(value).is_absolute():
                    raise ValueError
                path = Path(value).resolve(strict=True)
                identity = _identity(path)
                if not stat.S_ISREG(identity[-1]) or not os.access(path, os.X_OK):
                    raise ValueError
                self._programs[name] = (path, identity)
        except (OSError, ValueError, TypeError, RuntimeError):
            raise KernelError("process_binding_invalid", "cwd或可执行文件绑定无效") from None
        self._closed = False
        self._active: tuple[_Operation, asyncio.Task[ProcessResult]] | None = None

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("process_closed", "进程宿主已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._active is None:
            return
        operation, task = self._active
        operation.stop("closed")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain(task)
            raise
        except Exception:
            # 错误由原run调用交付，关闭不重复输出命令/原始异常。
            pass

    async def run(self, request: ProcessRequest, cancel: CancelToken) -> ProcessResult:
        cancel.checkpoint()
        if self._closed:
            raise KernelError("process_closed", "进程宿主已关闭")
        if self._active is not None:
            raise KernelError("process_busy", "进程宿主忙；未隐式排队或刷新截止时间")
        try:
            request = ProcessRequest.model_validate_json(request.model_dump_json(warnings="error"))
        except (ValidationError, ValueError):
            raise KernelError("process_invalid_arguments", "进程请求不符合契约") from None
        if request.program not in self._programs:
            raise KernelError("process_program_denied", "程序不在宿主绑定表内")
        if request.timeout_seconds > self._limits.max_timeout_seconds:
            raise KernelError("process_budget_exceeded", "请求超时超过宿主上限")
        started = asyncio.get_running_loop().time()
        operation = _Operation()
        task = asyncio.create_task(self._execute(request, operation, started))
        self._active = operation, task
        try:
            return await cancel.run(asyncio.shield(task))
        except TurnCancelled:
            operation.stop("cancelled")
            await _drain(task)
            return task.result()
        except asyncio.CancelledError:
            operation.stop("cancelled")
            await _drain(task)
            raise
        finally:
            self._active = None

    def _check_binding(self, program: str) -> Path:
        path, expected = self._programs[program]
        try:
            if _identity(self._cwd)[:2] != self._cwd_identity or _identity(path) != expected:
                raise ValueError
        except (OSError, ValueError):
            raise KernelError(
                "process_binding_changed", "cwd或程序身份已变化；未启动进程"
            ) from None
        return path

    async def _execute(
        self, request: ProcessRequest, operation: _Operation, started: float
    ) -> ProcessResult:
        loop = asyncio.get_running_loop()
        deadline = started + request.timeout_seconds
        if operation.reason is not None:
            raise TurnCancelled
        executable = self._check_binding(request.program)
        if loop.time() >= deadline:
            raise KernelError("process_timeout", "启动前截止时间已耗尽")
        capture = CaptureProtocol(self._limits, operation.stop)
        try:
            transport, _ = await loop.subprocess_exec(
                lambda: capture,
                str(executable),
                *request.arguments,
                cwd=self._cwd,
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise KernelError(
                "process_launch_failed", "进程启动失败；未记录原始参数或异常"
            ) from None
        stopper = asyncio.create_task(operation.stopped.wait())
        try:
            waiters: set[asyncio.Future[None] | asyncio.Task[bool]] = {capture.exited, stopper}
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                operation.stop("timeout")
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            termination = await self._settle(transport, capture)
        code = transport.get_returncode()
        assert code is not None  # process_exited在asyncio回收直接子进程之后通知。
        if termination == "failed":
            self._closed = True
        return ProcessResult(
            pid=transport.get_pid(),
            returncode=code,
            stop_reason="cleanup_failed"
            if termination == "failed"
            else operation.reason or "exited",
            termination=termination,
            stdout=capture.streams[1].result(),
            stderr=capture.streams[2].result(),
            elapsed_seconds=loop.time() - started,
        )

    @staticmethod
    def _group_exists(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _signal(pid: int, number: int) -> bool:
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        return True

    async def _settle(
        self, transport: asyncio.SubprocessTransport, capture: CaptureProtocol
    ) -> Literal["none", "term", "kill", "failed"]:
        pid = transport.get_pid()
        termination: Literal["none", "term", "kill", "failed"] = "none"
        if self._group_exists(pid):
            termination = "term" if self._signal(pid, signal.SIGTERM) else "failed"
            await asyncio.sleep(self._limits.terminate_grace_seconds)
            if self._group_exists(pid):
                killed = self._signal(pid, signal.SIGKILL)
                termination = "kill" if killed and termination != "failed" else "failed"
        if not capture.exited.done():
            try:
                transport.kill()
            except ProcessLookupError:
                pass
            except OSError:
                termination = "failed"
        # 不取消wait/reap；不可中断的内核状态没有Python级硬实时保证。
        await capture.exited
        try:
            await asyncio.wait_for(asyncio.shield(capture.closed), self._limits.pipe_drain_seconds)
        except TimeoutError:
            capture.close_pipes()
        finally:
            transport.close()
        await capture.closed
        return termination
