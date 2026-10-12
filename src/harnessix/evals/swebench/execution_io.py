"""SWE-bench Agent 执行状态的受限、原子 0600 JSON 持久化。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    SweBenchSuiteExecutionState,
    validate_swebench_case_execution_state,
    validate_swebench_suite_execution_state,
)

_MAX_EXECUTION_STATE_BYTES = 1024 * 1024


def _write_state(path: Path, body: bytes, *, error_code: str, label: str) -> None:
    if not 1 <= len(body) <= _MAX_EXECUTION_STATE_BYTES:
        raise KernelError(error_code, f"{label}无效或超过大小上限")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        parent = path.parent.resolve(strict=True)
        target = parent / path.name
        if target.is_symlink():
            raise OSError
        temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise KernelError(error_code, f"{label}写入失败") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_state(path: Path, *, error_code: str, label: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= _MAX_EXECUTION_STATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = _MAX_EXECUTION_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size:
            raise OSError
        return body
    except OSError:
        raise KernelError(error_code, f"{label}缺失、损坏、权限错误或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_case_execution_state(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
    state: SweBenchCaseExecutionState,
) -> None:
    try:
        checked = validate_swebench_case_execution_state(plan, state)
    except ValueError:
        raise KernelError(
            "swebench_case_execution_state_invalid",
            "SWE-bench Case 执行状态无效",
        ) from None
    body = (checked.model_dump_json(indent=2) + "\n").encode("utf-8")
    _write_state(
        path,
        body,
        error_code="swebench_case_execution_state_write_failed",
        label="SWE-bench Case 执行状态",
    )


def read_case_execution_state(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
) -> SweBenchCaseExecutionState:
    body = _read_state(
        path,
        error_code="swebench_case_execution_state_invalid",
        label="SWE-bench Case 执行状态",
    )
    try:
        state = SweBenchCaseExecutionState.model_validate_json(body, strict=True)
        return validate_swebench_case_execution_state(plan, state)
    except ValueError:
        raise KernelError(
            "swebench_case_execution_state_invalid",
            "SWE-bench Case 执行状态无效",
        ) from None


def write_suite_execution_state(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
    state: SweBenchSuiteExecutionState,
) -> None:
    try:
        checked = validate_swebench_suite_execution_state(plan, state)
    except ValueError:
        raise KernelError(
            "swebench_suite_execution_state_invalid",
            "SWE-bench Suite 执行状态无效",
        ) from None
    body = (checked.model_dump_json(indent=2) + "\n").encode("utf-8")
    _write_state(
        path,
        body,
        error_code="swebench_suite_execution_state_write_failed",
        label="SWE-bench Suite 执行状态",
    )


def read_suite_execution_state(
    path: Path,
    plan: SweBenchPreRegistrationPlan,
) -> SweBenchSuiteExecutionState:
    body = _read_state(
        path,
        error_code="swebench_suite_execution_state_invalid",
        label="SWE-bench Suite 执行状态",
    )
    try:
        state = SweBenchSuiteExecutionState.model_validate_json(body, strict=True)
        return validate_swebench_suite_execution_state(plan, state)
    except ValueError:
        raise KernelError(
            "swebench_suite_execution_state_invalid",
            "SWE-bench Suite 执行状态无效",
        ) from None
