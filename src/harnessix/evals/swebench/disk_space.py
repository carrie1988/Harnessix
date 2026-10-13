"""评测写入前保留本机余量；不自动删除 Workspace、证据或 Docker 数据。"""

from __future__ import annotations

import shutil
from pathlib import Path

from harnessix.agent.errors import KernelError

MIN_EVALUATION_FREE_BYTES = 10 * 1024**3


def require_evaluation_disk_space(*paths: Path) -> None:
    """检查每个写入路径所在文件系统；新目录使用最近已存在的父路径。"""
    for path in paths:
        existing = path
        while True:
            try:
                free = shutil.disk_usage(existing).free
                break
            except FileNotFoundError:
                if existing.parent == existing:
                    raise KernelError(
                        "swebench_disk_state_unavailable", "无法读取评测磁盘余量"
                    ) from None
                existing = existing.parent
            except OSError:
                raise KernelError(
                    "swebench_disk_state_unavailable", "无法读取评测磁盘余量"
                ) from None
        if free < MIN_EVALUATION_FREE_BYTES:
            raise KernelError(
                "swebench_disk_space_low", "磁盘可用空间不足10 GiB，停止新增评测写入及付费调用"
            )
