"""在真实模型请求前核对预注册的 Harnessix 执行源码。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from harnessix.agent.errors import KernelError

_TIMEOUT_SECONDS = 30
_BOUND_PATHS = ("src/harnessix", "pyproject.toml")


def require_harnessix_execution_revision(
    source_root: Path,
    git_executable: Path,
    revision: str,
) -> None:
    """允许后续只改计划/文档，但执行源码必须与预注册提交逐字一致。"""

    root = source_root.resolve(strict=True)
    git = git_executable.resolve(strict=True)
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    if _text(git, root, environment, "rev-parse", "--show-toplevel") != str(root):
        raise KernelError("swebench_source_revision_mismatch", "Harnessix源码根不匹配")
    _require_success(git, root, environment, "cat-file", "-e", f"{revision}^{{commit}}")
    _require_success(git, root, environment, "merge-base", "--is-ancestor", revision, "HEAD")
    if _text(
        git,
        root,
        environment,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *_BOUND_PATHS,
    ):
        raise KernelError("swebench_source_revision_mismatch", "Harnessix执行源码存在未提交变更")
    result = _run(git, root, environment, "diff", "--quiet", revision, "--", *_BOUND_PATHS)
    if result.returncode != 0:
        raise KernelError("swebench_source_revision_mismatch", "Harnessix执行源码偏离预注册提交")


def _text(
    git: Path,
    root: Path,
    environment: dict[str, str],
    *arguments: str,
) -> str:
    result = _run(git, root, environment, *arguments, capture=True)
    if result.returncode != 0:
        raise KernelError("swebench_source_revision_mismatch", "无法核对Harnessix执行源码")
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError:
        raise KernelError("swebench_source_revision_mismatch", "Harnessix Git输出无效") from None


def _require_success(
    git: Path,
    root: Path,
    environment: dict[str, str],
    *arguments: str,
) -> None:
    if _run(git, root, environment, *arguments).returncode != 0:
        raise KernelError("swebench_source_revision_mismatch", "Harnessix预注册提交无效")


def _run(
    git: Path,
    root: Path,
    environment: dict[str, str],
    *arguments: str,
    capture: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            (str(git), "--no-pager", "-c", "core.hooksPath=/dev/null", *arguments),
            cwd=root,
            env={**environment, "HOME": os.devnull},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError(
            "swebench_source_revision_mismatch", "无法核对Harnessix执行源码"
        ) from None
