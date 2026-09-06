"""Coding Eval运行恢复状态的受限原子持久化。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.evals.contracts import CodingEvalRunState

MAX_EVAL_RUN_STATE_BYTES = 256 * 1024


def write_eval_run_state(path: Path, state: CodingEvalRunState) -> None:
    state = CodingEvalRunState.model_validate_json(state.model_dump_json(), strict=True)
    body = (state.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > MAX_EVAL_RUN_STATE_BYTES:
        raise KernelError("eval_run_state_too_large", "Eval运行状态超过大小上限")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        parent = path.parent.resolve(strict=True)
        target = parent / path.name
        if target.is_symlink():
            raise KernelError("eval_run_state_path_denied", "Eval运行状态目标不能是符号链接")
        temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
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
        os.replace(temporary, target)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except KernelError:
        raise
    except OSError:
        raise KernelError("eval_run_state_write_failed", "Eval运行状态写入失败") from None
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


def read_eval_run_state(path: Path) -> CodingEvalRunState:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o777 != 0o600
            or not 1 <= info.st_size <= MAX_EVAL_RUN_STATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = MAX_EVAL_RUN_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size:
            raise OSError
        return CodingEvalRunState.model_validate_json(body, strict=True)
    except (OSError, ValueError, ValidationError):
        raise KernelError("eval_run_state_invalid", "Eval运行状态缺失、损坏或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
