"""将Soak样本写入有界JSONL文件，并从落盘字节复核统计。"""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts.soak_samples import ScenarioId, SoakQuantiles, SoakSample, validate_sample_series

SAMPLE_FILENAME = "samples.jsonl"
MAX_SAMPLE_FILE_BYTES = 8 * 1024 * 1024
MAX_SAMPLE_COUNT = 100_000


def _canonical_body(samples: tuple[SoakSample, ...]) -> bytes:
    if not samples or len(samples) > MAX_SAMPLE_COUNT:
        raise KernelError("soak_samples_invalid", "Soak样本数量无效")
    body = b"\n".join(
        sample.model_dump_json(exclude_none=True).encode("utf-8") for sample in samples
    )
    body += b"\n"
    if len(body) > MAX_SAMPLE_FILE_BYTES:
        raise KernelError("soak_samples_too_large", "Soak样本文件超过大小上限")
    return body


def sample_sha256(samples: tuple[SoakSample, ...]) -> str:
    """按文件规范计算摘要，供Manifest构造后由发布器复核。"""

    return sha256(_canonical_body(samples)).hexdigest()


def write_sample_file(
    run_directory: Path,
    samples: tuple[SoakSample, ...],
    *,
    run_id: str,
    scenario_id: ScenarioId,
    expected_measured: dict[str, int],
) -> tuple[str, dict[str, SoakQuantiles]]:
    """在独占Run目录中落盘样本；此步骤本身不发布Run。"""

    try:
        statistics = validate_sample_series(
            samples,
            run_id=run_id,
            scenario_id=scenario_id,
            expected_measured=expected_measured,
        )
    except ValueError:
        raise KernelError("soak_samples_invalid", "Soak样本序号或计数无效") from None
    body = _canonical_body(samples)
    target = run_directory / SAMPLE_FILENAME
    temporary = run_directory / f".{SAMPLE_FILENAME}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        if (
            not stat.S_ISDIR(run_directory.stat(follow_symlinks=False).st_mode)
            or target.exists()
            or target.is_symlink()
        ):
            raise OSError
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
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
        if target.exists() or target.is_symlink():
            raise OSError
        os.replace(temporary, target)
    except OSError:
        raise KernelError("soak_samples_write_failed", "Soak样本写入失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return sha256(body).hexdigest(), statistics


def read_sample_file(
    run_directory: Path,
    *,
    expected_sha256: str,
    run_id: str,
    scenario_id: ScenarioId,
    expected_measured: dict[str, int],
) -> tuple[tuple[SoakSample, ...], str, dict[str, SoakQuantiles]]:
    """有界重读样本文件并严格重算；发布状态由上层提交标记验证。"""

    target = run_directory / SAMPLE_FILENAME
    try:
        if not stat.S_ISDIR(run_directory.stat(follow_symlinks=False).st_mode):
            raise OSError
        info = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SAMPLE_FILE_BYTES:
            raise OSError
        body = target.read_bytes()
        if (
            len(body) != info.st_size
            or not body.endswith(b"\n")
            or sha256(body).hexdigest() != expected_sha256
        ):
            raise ValueError
        lines = body.splitlines()
        if not 0 < len(lines) <= MAX_SAMPLE_COUNT or any(not line for line in lines):
            raise ValueError
        samples = tuple(SoakSample.model_validate_json(line) for line in lines)
        if body != _canonical_body(samples):
            raise ValueError
        statistics = validate_sample_series(
            samples,
            run_id=run_id,
            scenario_id=scenario_id,
            expected_measured=expected_measured,
        )
    except (OSError, ValueError, ValidationError, UnicodeError, KernelError):
        raise KernelError("soak_samples_invalid", "Soak样本文件无效") from None
    return samples, sha256(body).hexdigest(), statistics
