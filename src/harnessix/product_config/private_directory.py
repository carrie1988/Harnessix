"""逐级创建只允许当前用户访问的目录。"""

from __future__ import annotations

import os
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700


def make_private_directories(path: Path) -> None:
    """创建path和它缺失的各级父目录，每一级都按0700创建；已有的目录不改动。

    `Path.mkdir(mode=0o700, parents=True)`只把权限用在最后一级，缺失的父目录按umask创建
    （通常是755）；`~/.harnessix`曾因此被建成755，之后`harnessix code configure`拒绝使用它。
    path最终不是目录时抛出OSError。
    """
    missing: list[Path] = []
    level = path
    while not os.path.lexists(level):
        missing.append(level)
        level = level.parent
    for level in reversed(missing):
        try:
            os.mkdir(level, PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            # 另一个进程刚建好这一级；它若不是目录，由下面的检查报错。
            pass
    if not path.is_dir():
        raise NotADirectoryError(str(path))
