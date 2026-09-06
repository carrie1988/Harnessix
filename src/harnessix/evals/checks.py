"""通过受信宿主程序运行内置历史任务检查并只返回摘要证据。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal
from uuid import uuid4

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.evals.catalog import HistoricalCodingEval
from harnessix.evals.contracts import EvalTestObservation
from harnessix.evals.materializer import MaterializedCodingEval
from harnessix.processes.contracts import ProcessLimits, ProcessRequest
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.tools.workspace import digest

CheckPhase = Literal["baseline", "final"]


def _read_launcher(path: Path, expected_bytes: int) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise OSError
        chunks: list[bytes] = []
        remaining = expected_bytes
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def historical_python_launcher(
    materialized: MaterializedCodingEval, python_executable: Path
) -> Path:
    """在工作区外绑定venv入口，避免解析符号链接后丢失Python虚拟环境语义。"""

    if not python_executable.is_absolute() or any(
        ord(character) < 32 or ord(character) == 127 for character in str(python_executable)
    ):
        raise KernelError("eval_python_binding_invalid", "Eval Python入口绑定无效")
    try:
        target = python_executable.resolve(strict=True)
        info = target.stat()
    except (OSError, RuntimeError):
        raise KernelError("eval_python_binding_invalid", "Eval Python入口绑定无效") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(target, os.X_OK):
        raise KernelError("eval_python_binding_invalid", "Eval Python入口绑定无效")
    escaped = str(python_executable).replace("'", "'\"'\"'")
    body = f"#!/bin/sh\nexec '{escaped}' \"$@\"\n".encode()
    host = materialized.run_root / "host"
    launcher = host / "python"
    temporary = host / f".python.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        if host.exists():
            info = host.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o777 != 0o700:
                raise OSError
        else:
            host.mkdir(mode=0o700)
        if launcher.exists() or launcher.is_symlink():
            if _read_launcher(launcher, len(body)) != body:
                raise OSError
            launcher.chmod(0o700)
            return launcher
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o700,
        )
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, launcher)
        directory = os.open(host, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return launcher
    except OSError:
        raise KernelError("eval_python_launcher_failed", "Eval Python入口发布或复核失败") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass


def historical_check_arguments(workspace: Path, mode: str) -> tuple[str, ...]:
    """供隐藏检查和后续run_tests Profile复用的固定argv。"""

    checker = Path(__file__).with_name("_historical_check.py").resolve(strict=True)
    return "-I", "-B", str(checker), str(workspace), mode


async def run_historical_checks(
    definition: HistoricalCodingEval,
    materialized: MaterializedCodingEval,
    python_executable: Path,
    phase: CheckPhase,
    cancel: CancelToken | None = None,
) -> tuple[EvalTestObservation, ...]:
    """运行任务固定检查；退出码0/1是行为事实，其他终态属于基础设施失败。"""

    task = definition.task
    manifest = materialized.manifest
    if (
        manifest.task_fingerprint != task.fingerprint
        or manifest.baseline_tree_sha256 != task.repository.baseline_tree_sha256
    ):
        raise KernelError("eval_materialization_mismatch", "Eval检查工作区与任务身份不一致")
    names = (
        task.baseline_checks
        if phase == "baseline"
        else tuple(sorted((*task.behavior_checks, *task.regression_checks)))
    )
    token = cancel or CancelToken()
    launcher = historical_python_launcher(materialized, python_executable)
    observations: list[EvalTestObservation] = []
    limits = ProcessLimits(
        max_timeout_seconds=60,
        stdout_bytes=16 * 1024,
        stderr_bytes=16 * 1024,
        stop_output_bytes=128 * 1024,
    )
    async with HostProcessRuntime(
        materialized.workspace,
        {"python": launcher},
        limits=limits,
    ) as runtime:
        for name in names:
            check = definition.check(name)
            result = await runtime.run(
                ProcessRequest(
                    program="python",
                    arguments=historical_check_arguments(materialized.workspace, check.mode),
                    timeout_seconds=60,
                ),
                token,
            )
            if result.stop_reason == "cancelled":
                raise TurnCancelled
            if (
                result.stop_reason != "exited"
                or result.returncode not in {0, 1}
                or not result.stdout.eof
                or not result.stderr.eof
            ):
                raise KernelError(
                    "eval_check_infrastructure_failed", "Eval历史检查未形成确定退出证据"
                )
            observations.append(
                EvalTestObservation(
                    check_id=name,
                    phase=phase,
                    passed=result.returncode == 0,
                    returncode=result.returncode,
                    output_sha256=digest(
                        {
                            "stdout_sha256": result.stdout.observed_sha256,
                            "stdout_bytes": result.stdout.observed_bytes,
                            "stderr_sha256": result.stderr.observed_sha256,
                            "stderr_bytes": result.stderr.observed_bytes,
                            "returncode": result.returncode,
                        }
                    ),
                    elapsed_seconds=result.elapsed_seconds,
                )
            )
    return tuple(observations)
