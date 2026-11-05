"""工作区一次性可写快照的复制与授权；沙箱容器启动和候选交叉检查共用。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def copy_tree(source: Path, target: Path) -> None:
    """尽量用文件系统克隆（XFS/Btrfs reflink、APFS clonefile）复制，退化为普通复制。"""

    if sys.platform == "darwin":
        completed = subprocess.run(
            ["/bin/cp", "-c", "-R", "-p", str(source), str(target)],
            capture_output=True,
            timeout=600,
            check=False,
        )
        if completed.returncode == 0:
            return
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, symlinks=True)
        return
    target.mkdir(mode=0o700)
    subprocess.run(
        ["cp", "-a", "--reflink=auto", f"{source}/.", str(target)],
        capture_output=True,
        timeout=600,
        check=True,
    )


def grant_sandbox_write(target: Path, run_as: str) -> None:
    """以root运行时把副本交给沙箱用户；否则只能放宽副本自身的权限（父目录不可列举）。"""

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        subprocess.run(
            ["chown", "-R", "--no-dereference", run_as, str(target)]
            if sys.platform != "darwin"
            else ["chown", "-R", "-h", run_as, str(target)],
            capture_output=True,
            timeout=600,
            check=True,
        )
        return
    subprocess.run(
        ["chmod", "-R", "a+rwX", str(target)], capture_output=True, timeout=600, check=True
    )
