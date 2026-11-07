"""原生进程沙箱（ADR 0111）：把沙箱策略翻译成bubblewrap参数，并准备每条命令的启动现场。

翻译顺序决定覆盖关系：只读根 → 新/dev、/proc → 私有/tmp、/run → 可写目录 → `.git`只读 →
拒读遮盖。后挂载的覆盖先挂载的，因此可写目录内的`.git`与拒读路径始终生效。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.sandbox.native_contracts import NativeSandboxPolicy
from harnessix.sandbox.native_policy import is_workspace_env_file

# 与ProcessSpec.argv的项数上限一致（测试守护两者不漂移）。
PROCESS_ARGV_LIMIT = 128
_FIXED_FLAGS = ("--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL")
# 宿主运行时目录：含Docker、D-Bus、ssh-agent等Unix套接字，换成私有tmpfs。
_HIDDEN_RUNTIME_ROOTS = ("/tmp", "/run", "/var/run")
_GIT_TIMEOUT_SECONDS = 10.0
_GIT_OUTPUT_LIMIT = 64 * 1024 * 1024
_WALK_MAX_DEPTH = 8
_WALK_MAX_ENTRIES = 50_000
# 非Git工作区不深入的依赖与构建目录，对应Git工作区“整体被忽略的目录不深入”。
_WALK_SKIPPED_DIRECTORIES = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist", ".tox"}
)
EMPTY_MASK_DIRECTORY = ".empty"


@dataclass(frozen=True, slots=True)
class BubblewrapMasks:
    """启动前确定的遮盖对象：目录用空只读目录覆盖，文件用/dev/null覆盖。"""

    directories: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


def hidden_runtime_roots() -> tuple[str, ...]:
    """宿主上真实存在的运行时目录；符号链接（如/var/run→/run）随目标一并隐藏。"""

    return tuple(
        root for root in _HIDDEN_RUNTIME_ROOTS if os.path.isdir(root) and not os.path.islink(root)
    )


def denied_path_masks(policy: NativeSandboxPolicy) -> BubblewrapMasks:
    """只遮盖实际存在的拒读路径；符号链接按真实目标遮盖。"""

    directories: set[str] = set()
    files: set[str] = set()
    for candidate in policy.denied_paths:
        if not os.path.lexists(candidate):
            continue
        target = os.path.realpath(candidate)
        if os.path.isdir(target):
            directories.add(target)
        elif os.path.exists(target):
            files.add(target)
    return BubblewrapMasks(tuple(sorted(directories)), tuple(sorted(files)))


def bubblewrap_argv(
    policy: NativeSandboxPolicy,
    *,
    executable: str,
    command: Sequence[str],
    masks: BubblewrapMasks,
    env_files: Sequence[str] = (),
    hidden_roots: Sequence[str] = ("/tmp", "/run"),
) -> tuple[str, ...]:
    """翻译完整argv；超出ProcessSpec项数上限时失败关闭，不截断任何遮盖。"""

    if policy.backend != "bubblewrap":
        raise KernelError("sandbox_backend_mismatch", "策略不属于bubblewrap后端")
    empty = os.path.join(policy.private_cache, EMPTY_MASK_DIRECTORY)
    base: list[str] = [executable, *_FIXED_FLAGS, "--ro-bind", "/", "/"]
    base += ["--dev", "/dev", "--proc", "/proc"]
    for root in hidden_roots:
        base += ["--tmpfs", root]
    for root in policy.writable_roots:
        base += ["--bind", root, root]
    for root in policy.read_only_roots:
        base += ["--ro-bind", root, root]
    for directory in masks.directories:
        base += ["--ro-bind", empty, directory]
    for file in masks.files:
        base += ["--ro-bind", "/dev/null", file]
    tail = ["--chdir", policy.workspace, "--", *command]
    if len(base) + len(tail) > PROCESS_ARGV_LIMIT:
        raise KernelError("sandbox_policy_too_large", "沙箱绑定与遮盖超出进程参数上限")
    env_masks: list[str] = []
    for file in env_files:
        env_masks += ["--ro-bind", "/dev/null", file]
    if len(base) + len(env_masks) + len(tail) > PROCESS_ARGV_LIMIT:
        raise KernelError("workspace_secret_files_unmaskable", "工作区.env文件过多，超出遮盖容量")
    return (*base, *env_masks, *tail)


def prepare_private_directories(policy: NativeSandboxPolicy) -> None:
    """每条命令前清空私有TMPDIR，并确保私有缓存与空遮盖目录存在。"""

    tmp = Path(policy.private_tmp)
    cache = Path(policy.private_cache)
    try:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        empty = cache / EMPTY_MASK_DIRECTORY
        empty.mkdir(mode=0o500, exist_ok=True)
        if any(empty.iterdir()):
            raise KernelError("sandbox_path_invalid", "空遮盖目录不为空")
        if tmp.is_symlink():
            raise KernelError("sandbox_path_invalid", "私有临时目录不能是符号链接")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(mode=0o700, parents=True)
    except OSError:
        raise KernelError("sandbox_path_invalid", "私有临时或缓存目录无法准备") from None


def workspace_env_files(workspace: Path, *, git: Path | None) -> tuple[str, ...]:
    """查找工作区内需要遮盖的`.env`文件（ADR 0111第2.3节）。

    Git工作区列出已跟踪、未跟踪与被忽略的文件，整体被忽略的目录不深入；
    非Git工作区跳过常见依赖与构建目录、只查8层以内，条目超限时失败关闭。
    """

    if git is not None:
        listed = _git_env_files(workspace, git)
        if listed is not None:
            return listed
    return _walk_env_files(workspace)


# 已跟踪文件，以及按忽略规则逐个列出的未跟踪/被忽略文件；整体被忽略的目录只列一项、不深入。
_GIT_LISTINGS = (
    ("ls-files", "-z", "--cached"),
    (
        "status",
        "--porcelain=v1",
        "-z",
        "--ignored=matching",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ),
)


def _git_env_files(workspace: Path, git: Path) -> tuple[str, ...] | None:
    paths: set[str] = set()
    for listing in _GIT_LISTINGS:
        argv = (str(git), "-c", "core.fsmonitor=false", "-C", str(workspace), *listing)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
                env={
                    "PATH": os.pathsep.join((str(git.parent), os.defpath)),
                    "LANG": "C",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        if len(completed.stdout) > _GIT_OUTPUT_LIMIT:
            raise KernelError("workspace_secret_files_unmaskable", "工作区文件清单超出查找上限")
        for raw in completed.stdout.split(b"\0"):
            if listing[0] == "status":
                # 只取未跟踪（??）与被忽略（!!）条目；已跟踪文件由ls-files覆盖。
                if raw[:3] not in {b"?? ", b"!! "}:
                    continue
                raw = raw[3:]
            if not raw or raw.endswith(b"/"):
                continue
            path = workspace / os.fsdecode(raw)
            if is_workspace_env_file(path.name) and path.is_file():
                paths.add(str(path))
    return tuple(sorted(paths))


def _walk_env_files(workspace: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    seen = 0
    root_depth = len(workspace.parts)
    for directory, subdirectories, files in os.walk(workspace, followlinks=False):
        current = Path(directory)
        seen += len(subdirectories) + len(files)
        if seen > _WALK_MAX_ENTRIES:
            raise KernelError(
                "workspace_secret_files_unmaskable", "非Git工作区文件过多，无法完成.env查找"
            )
        subdirectories[:] = [
            name for name in subdirectories if name not in _WALK_SKIPPED_DIRECTORIES
        ]
        if len(current.parts) - root_depth >= _WALK_MAX_DEPTH:
            subdirectories[:] = []
        for name in files:
            path = current / name
            if is_workspace_env_file(name) and path.is_file():
                paths.add(str(path))
    return tuple(sorted(paths))
