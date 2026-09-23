"""正式Soak发布入口共用的子进程硬期限与低敏错误边界。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def guarded_worker(
    module: str,
    *,
    arguments: tuple[str, ...],
    repository: Path,
    timeout_seconds: int,
    error_prefix: str,
) -> tuple[str | None, str | None]:
    """子进程可能排空无界SQLite任务；超时整体杀死并只返回稳定错误码。"""

    try:
        completed = subprocess.run(
            (sys.executable, "-m", module, "--internal-worker", *arguments),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "soak_worker_timeout"
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None, "soak_worker_failed"
    if completed.returncode != 0:
        try:
            safe_stderr = len(completed.stderr.encode("utf-8")) <= 256
        except UnicodeError:
            safe_stderr = False
        match = (
            re.fullmatch(rf"{re.escape(error_prefix)}：([a-z][a-z0-9_]*)\n", completed.stderr)
            if safe_stderr
            else None
        )
        return None, match.group(1) if match is not None else "soak_worker_failed"
    if completed.stderr:
        return None, "soak_worker_invalid"
    return completed.stdout, None
