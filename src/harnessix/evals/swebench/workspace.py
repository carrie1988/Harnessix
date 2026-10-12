"""Materialize public SWE-bench repositories without exposing evaluator data."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import SweBenchPublicCase

_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 120
_NETWORK_GIT_TIMEOUT_SECONDS = 600
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/usr/bin/false",
    "SSH_ASKPASS": "/usr/bin/false",
    "GCM_INTERACTIVE": "Never",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_GIT_GLOBAL_ARGUMENTS = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "color.ui=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=never",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "submodule.recurse=false",
)
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes


def _git_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise KernelError("swebench_git_invalid", "SWE-bench Git 绑定无效") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise KernelError("swebench_git_invalid", "SWE-bench Git 绑定无效")
    return resolved


def _invoke_git(
    executable: Path,
    root: Path,
    arguments: tuple[str, ...],
    *,
    max_bytes: int = _GIT_OUTPUT_BYTES,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> _GitResult:
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                (str(executable), *_GIT_GLOBAL_ARGUMENTS, *arguments),
                cwd=root,
                env=_GIT_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=timeout,
            )
            size = output.tell()
            if size > max_bytes:
                raise KernelError("swebench_git_failed", "SWE-bench Git 输出超过上限")
            output.seek(0)
            return _GitResult(completed.returncode, output.read(max_bytes + 1))
    except KernelError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise KernelError("swebench_git_failed", "SWE-bench 固定 Git 命令失败") from None


def _git(
    executable: Path,
    root: Path,
    arguments: tuple[str, ...],
    *,
    max_bytes: int = _GIT_OUTPUT_BYTES,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> bytes:
    result = _invoke_git(
        executable,
        root,
        arguments,
        max_bytes=max_bytes,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise KernelError("swebench_git_failed", "SWE-bench 固定 Git 命令失败")
    return result.stdout


def _git_text(executable: Path, root: Path, arguments: tuple[str, ...]) -> str:
    try:
        return (
            _git(executable, root, arguments, max_bytes=4096)
            .decode("utf-8", errors="strict")
            .removesuffix("\n")
        )
    except UnicodeError:
        raise KernelError("swebench_git_failed", "SWE-bench Git 输出不是 UTF-8") from None


def _private_directory(path: Path, *, error_code: str, create: bool) -> Path:
    created = False
    try:
        if create:
            try:
                os.mkdir(path, 0o700)
                created = True
            except FileNotFoundError:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.mkdir(path, 0o700)
                created = True
            except FileExistsError:
                pass
        lexical = path.lstat()
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if created:
            descriptor = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
            info = resolved.stat()
    except (OSError, RuntimeError):
        raise KernelError(error_code, "SWE-bench 私有目录无效") from None
    if (
        stat.S_ISLNK(lexical.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise KernelError(error_code, "SWE-bench 私有目录必须是当前用户的 0700 目录")
    return resolved


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise KernelError("swebench_workspace_changed", "SWE-bench 工作区路径无效") from None


def _validated_case(case: SweBenchPublicCase) -> tuple[str, str, str]:
    instance_id = case.instance_id
    repository = case.repo
    base_commit = case.base_commit
    if (
        not isinstance(instance_id, str)
        or _INSTANCE_ID.fullmatch(instance_id) is None
        or not isinstance(repository, str)
        or _REPOSITORY.fullmatch(repository) is None
        or not isinstance(base_commit, str)
        or _COMMIT.fullmatch(base_commit) is None
    ):
        raise KernelError("swebench_workspace_case_invalid", "SWE-bench Case 身份无效")
    return instance_id, repository, base_commit


def _repository_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _cache_path(cache_root: Path, repository: str) -> Path:
    identity = hashlib.sha256(repository.encode("ascii", errors="strict")).hexdigest()
    return cache_root / f"{identity}.git"


def _commit_is_available(executable: Path, repository: Path, commit: str) -> bool:
    result = _invoke_git(
        executable,
        repository,
        ("rev-parse", "--verify", f"{commit}^{{commit}}"),
        max_bytes=4096,
    )
    if result.returncode != 0:
        return False
    try:
        observed = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeError:
        return False
    return observed == commit


def _validate_cache(
    executable: Path,
    cache: Path,
    expected_url: str,
    base_commit: str,
) -> Path:
    root = _private_directory(
        cache,
        error_code="swebench_workspace_cache_changed",
        create=False,
    )
    if (
        _git_text(executable, root, ("rev-parse", "--absolute-git-dir")) != str(root)
        or _git_text(executable, root, ("rev-parse", "--is-bare-repository")) != "true"
        or _git_text(
            executable,
            root,
            ("config", "--local", "--get-all", "remote.origin.url"),
        )
        != expected_url
        or not _commit_is_available(executable, root, base_commit)
    ):
        raise KernelError(
            "swebench_workspace_cache_changed",
            "SWE-bench 公共仓库缓存身份已漂移",
        )
    return root


def _clone_cache(
    executable: Path,
    cache_root: Path,
    cache: Path,
    expected_url: str,
    base_commit: str,
) -> Path:
    staging = cache_root / f".{cache.name}.{uuid4().hex}.tmp"
    try:
        os.mkdir(staging, 0o700)
        _git(
            executable,
            cache_root,
            (
                "clone",
                "--bare",
                "--no-recurse-submodules",
                expected_url,
                str(staging),
            ),
            timeout=_NETWORK_GIT_TIMEOUT_SECONDS,
        )
        _validate_cache(executable, staging, expected_url, base_commit)
        os.rename(staging, cache)
        return _validate_cache(executable, cache, expected_url, base_commit)
    except KernelError:
        raise
    except OSError:
        raise KernelError(
            "swebench_workspace_cache_failed",
            "SWE-bench 公共仓库缓存创建失败",
        ) from None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _load_or_create_cache(
    executable: Path,
    cache_root: Path,
    repository: str,
    base_commit: str,
    *,
    allow_network: bool,
) -> Path:
    expected_url = _repository_url(repository)
    cache = _cache_path(cache_root, repository)
    if not _path_exists(cache):
        if allow_network is not True:
            raise KernelError(
                "swebench_workspace_network_disabled",
                "禁网模式下不存在可复用的 SWE-bench 公共仓库缓存",
            )
        return _clone_cache(executable, cache_root, cache, expected_url, base_commit)

    root = _private_directory(
        cache,
        error_code="swebench_workspace_cache_changed",
        create=False,
    )
    if (
        _git_text(executable, root, ("rev-parse", "--absolute-git-dir")) != str(root)
        or _git_text(executable, root, ("rev-parse", "--is-bare-repository")) != "true"
        or _git_text(
            executable,
            root,
            ("config", "--local", "--get-all", "remote.origin.url"),
        )
        != expected_url
    ):
        raise KernelError(
            "swebench_workspace_cache_changed",
            "SWE-bench 公共仓库缓存身份已漂移",
        )
    if _commit_is_available(executable, root, base_commit):
        return root
    if allow_network is not True:
        raise KernelError(
            "swebench_workspace_network_disabled",
            "禁网模式下 SWE-bench 公共仓库缓存缺少目标提交",
        )
    _git(
        executable,
        root,
        ("fetch", "--force", "--no-tags", "origin", "+refs/heads/*:refs/heads/*"),
        timeout=_NETWORK_GIT_TIMEOUT_SECONDS,
    )
    if not _commit_is_available(executable, root, base_commit):
        raise KernelError(
            "swebench_workspace_revision_unavailable",
            "SWE-bench 公共仓库不包含目标提交",
        )
    return root


def _alternates_body(cache: Path) -> bytes:
    body = os.fsencode(cache / "objects")
    if b"\n" in body or b"\r" in body:
        raise KernelError(
            "swebench_workspace_cache_changed",
            "SWE-bench 缓存路径不能写入 Git alternates",
        )
    return body + b"\n"


def _write_alternates(workspace: Path, cache: Path) -> None:
    path = workspace / ".git" / "objects" / "info" / "alternates"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        body = memoryview(_alternates_body(cache))
        while body:
            written = os.write(descriptor, body)
            if written <= 0:
                raise OSError
            body = body[written:]
        os.fsync(descriptor)
    except OSError:
        raise KernelError(
            "swebench_workspace_create_failed",
            "SWE-bench 工作区对象缓存绑定失败",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_alternates(workspace: Path, cache: Path) -> None:
    path = workspace / ".git" / "objects" / "info" / "alternates"
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        body = os.read(descriptor, 4097)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
            or body != _alternates_body(cache)
        ):
            raise OSError
    except OSError:
        raise KernelError(
            "swebench_workspace_changed",
            "SWE-bench 工作区对象缓存绑定已漂移",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_workspace(
    executable: Path,
    workspace: Path,
    cache: Path,
    expected_url: str,
    base_commit: str,
    *,
    require_clean: bool,
) -> Path:
    root = _private_directory(
        workspace,
        error_code="swebench_workspace_changed",
        create=False,
    )
    git_directory = root / ".git"
    try:
        git_info = git_directory.lstat()
    except OSError:
        raise KernelError("swebench_workspace_changed", "SWE-bench 工作区 Git 目录无效") from None
    if not stat.S_ISDIR(git_info.st_mode) or stat.S_ISLNK(git_info.st_mode):
        raise KernelError("swebench_workspace_changed", "SWE-bench 工作区 Git 目录无效")
    _validate_alternates(root, cache)
    status = _git(
        executable,
        root,
        ("status", "--porcelain=v2", "--untracked-files=all", "-z"),
    )
    if (
        _git_text(executable, root, ("rev-parse", "--show-toplevel")) != str(root)
        or _git_text(executable, root, ("rev-parse", "--verify", "HEAD^{commit}")) != base_commit
        or _git_text(executable, root, ("rev-parse", "--abbrev-ref", "HEAD")) != "HEAD"
        or _git_text(
            executable,
            root,
            ("config", "--local", "--get-all", "remote.origin.url"),
        )
        != expected_url
        or (require_clean and status)
    ):
        raise KernelError(
            "swebench_workspace_changed",
            "SWE-bench 工作区 HEAD、origin、detached 或 clean 状态已漂移",
        )
    return root


def _create_workspace(
    executable: Path,
    workspaces_root: Path,
    workspace: Path,
    cache: Path,
    expected_url: str,
    base_commit: str,
) -> Path:
    staging = workspaces_root / f".{workspace.name}.{uuid4().hex}.tmp"
    try:
        os.mkdir(staging, 0o700)
        _git(executable, staging, ("init", "--quiet"))
        _git(executable, staging, ("remote", "add", "origin", expected_url))
        _write_alternates(staging, cache)
        _git(
            executable,
            staging,
            ("checkout", "--quiet", "--detach", "--force", "--no-recurse-submodules", base_commit),
        )
        _validate_workspace(
            executable,
            staging,
            cache,
            expected_url,
            base_commit,
            require_clean=True,
        )
        os.rename(staging, workspace)
        return _validate_workspace(
            executable,
            workspace,
            cache,
            expected_url,
            base_commit,
            require_clean=True,
        )
    except KernelError:
        raise
    except OSError:
        raise KernelError(
            "swebench_workspace_create_failed",
            "SWE-bench 工作区创建失败",
        ) from None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def materialize_swebench_workspace(
    case: SweBenchPublicCase,
    *,
    workspaces_root: Path,
    cache_root: Path,
    git_executable: Path,
    allow_network: bool,
    require_clean: bool = True,
) -> Path:
    """Create or strictly reopen one clean detached public SWE-bench workspace.

    Only ``instance_id``, ``repo`` and ``base_commit`` are read from the public
    case. Repository objects are shared through a bare cache; neither evaluator
    patches nor the separate official task repository participate in this path.
    """

    executable = _git_executable(git_executable)
    instance_id, repository, base_commit = _validated_case(case)
    workspace_parent = _private_directory(
        workspaces_root,
        error_code="swebench_workspace_root_invalid",
        create=True,
    )
    cache_parent = _private_directory(
        cache_root,
        error_code="swebench_workspace_cache_root_invalid",
        create=True,
    )
    workspace = workspace_parent / instance_id
    expected_url = _repository_url(repository)

    if _path_exists(workspace):
        cache = _validate_cache(
            executable,
            _cache_path(cache_parent, repository),
            expected_url,
            base_commit,
        )
        return _validate_workspace(
            executable,
            workspace,
            cache,
            expected_url,
            base_commit,
            require_clean=require_clean,
        )

    cache = _load_or_create_cache(
        executable,
        cache_parent,
        repository,
        base_commit,
        allow_network=allow_network,
    )
    return _create_workspace(
        executable,
        workspace_parent,
        workspace,
        cache,
        expected_url,
        base_commit,
    )
