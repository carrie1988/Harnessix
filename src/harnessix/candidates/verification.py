"""在与候选运行相同的容器策略下交叉执行检查命令。

检查命令由宿主从候选已获批准并执行过的命令中选出，不是模型新提出的动作；它们在候选使用的同一镜像
与Process Profile限制下运行：只读根文件系统、只读工作区、无网络、非root、丢弃全部能力。
检查来自另一候选时，把该候选的草稿文件（复现脚本）叠加到被测补丁的工作区副本中。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harnessix.candidates.selection import Check, CheckResult
from harnessix.product_config.action_contracts import ProductProcessProfile
from harnessix.product_config.coding_scratch import is_scratch_path
from harnessix.sandbox.contracts import DEFAULT_CONTAINER_WORKSPACE
from harnessix.sandbox.workspace_snapshot import copy_tree, grant_sandbox_write

_SANDBOX_USER = "65532:65532"
_DEFAULT_TMPFS_BYTES = 64 * 1024 * 1024
_OUTPUT_TAIL_BYTES = 2048


@dataclass(frozen=True)
class CheckRun:
    candidate_id: str
    check_index: int
    result: CheckResult
    returncode: int | None
    seconds: float
    output_tail: str


def container_argv(profile: ProductProcessProfile, workspace: Path, cmd: str) -> list[str]:
    """与Product Process Profile一致的一次性容器命令；工作区只读挂载在Profile声明的路径。"""

    target = profile.workspace_path or DEFAULT_CONTAINER_WORKSPACE
    snapshot = profile.workspace_mode == "snapshot"
    environment = [f"{name}={value}" for name, value in profile.environment]
    return [
        profile.container_engine,
        "run",
        "--rm",
        "--init",
        "--pull",
        "never",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--ipc",
        "none",
        "--network",
        "none",
        "--pids-limit",
        str(profile.process_limit),
        "--memory",
        str(profile.memory_bytes),
        "--cpus",
        format(profile.cpu_limit, "g"),
        "--user",
        _SANDBOX_USER,
        "--workdir",
        target,
        "--mount",
        f"type=bind,src={workspace},dst={target}" + ("" if snapshot else ",readonly"),
        "--tmpfs",
        f"/tmp:rw,{'' if snapshot else 'noexec,'}nosuid,nodev,"
        f"size={profile.tmpfs_bytes or _DEFAULT_TMPFS_BYTES}",
        *(item for value in environment for item in ("--env", value)),
        "--ulimit",
        "nofile=1024:1024",
        "--label",
        "owner=harnessix",
        "--entrypoint",
        profile.program,
        profile.image,
        *profile.arguments,
        cmd,
    ]


def scratch_files(workspace: Path) -> list[Path]:
    """候选工作区中的草稿文件（相对路径），它们不进入交付补丁。"""

    found = []
    for root, directories, files in os.walk(workspace):
        directories[:] = [name for name in directories if name != ".git"]
        for name in files:
            relative = Path(root, name).relative_to(workspace)
            if is_scratch_path(relative.as_posix()):
                found.append(relative)
    return found


@contextmanager
def overlay_workspace(
    target: Path,
    scratch_source: Path | None,
    copy: Path,
    *,
    writable_for: str | None = None,
) -> Iterator[Path]:
    """为一次检查准备工作区：叠加另一候选的草稿文件，或为快照模式准备可写副本。

    只读挂载时用硬链接复制并以替换方式叠加草稿；可写快照必须写时复制，
    否则命令原地写入会改动被测候选的工作区。
    """

    if scratch_source is None and writable_for is None:
        yield target
        return
    shutil.rmtree(copy, ignore_errors=True)
    if writable_for is None:
        shutil.copytree(target, copy, copy_function=os.link, symlinks=True)
    else:
        copy_tree(target, copy)
    try:
        if scratch_source is not None:
            for relative in scratch_files(scratch_source):
                destination = copy / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    destination.unlink()  # 断开硬链接，不能改写被测工作区原文件
                shutil.copy2(scratch_source / relative, destination)
                destination.chmod(0o644)
        if writable_for is not None:
            grant_sandbox_write(copy, writable_for)
        yield copy
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def run_check(
    profile: ProductProcessProfile,
    workspace: Path,
    check: Check,
    *,
    candidate_id: str,
    check_index: int,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> CheckRun:
    started = time.monotonic()
    try:
        completed = runner(
            container_argv(profile, workspace, check.cmd),
            capture_output=True,
            timeout=profile.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckRun(candidate_id, check_index, "error", None, time.monotonic() - started, "")
    tail = (completed.stdout + completed.stderr)[-_OUTPUT_TAIL_BYTES:]
    result: CheckResult = "passed" if completed.returncode == 0 else "failed"
    # 125+是容器引擎自身的错误（镜像、挂载、权限），不是被测命令的结论。
    if completed.returncode >= 125:
        result = "error"
    return CheckRun(
        candidate_id,
        check_index,
        result,
        completed.returncode,
        round(time.monotonic() - started, 2),
        tail.decode("utf-8", errors="replace"),
    )


def cross_check(
    profile: ProductProcessProfile,
    workspaces: dict[str, Path],
    checks: tuple[Check, ...],
    scratch_root: Path,
    *,
    parallel: int = 4,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[CheckRun]:
    """对每个候选工作区运行全部检查；检查来自别的候选时叠加其草稿文件。"""

    def one(job: tuple[str, int]) -> CheckRun:
        candidate_id, index = job
        check = checks[index]
        source = None if check.origin == candidate_id else workspaces.get(check.origin)
        copy = scratch_root / f"{candidate_id}-check{index}"
        writable_for = _SANDBOX_USER if profile.workspace_mode == "snapshot" else None
        with overlay_workspace(
            workspaces[candidate_id], source, copy, writable_for=writable_for
        ) as workspace:
            return run_check(
                profile,
                workspace,
                check,
                candidate_id=candidate_id,
                check_index=index,
                runner=runner,
            )

    jobs = [(candidate_id, index) for candidate_id in workspaces for index in range(len(checks))]
    scratch_root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        return list(pool.map(one, jobs))
