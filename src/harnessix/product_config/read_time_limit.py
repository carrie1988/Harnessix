"""工作区读取（读文件、列目录、glob、grep）的时限：配置优先，未配置时按平台给默认值。

Windows第一次读到一个文件要先经Defender扫描：发布会话在Windows 11真机上测得约18毫秒/文件，
读过之后约0.15毫秒。刚克隆或很久没打开的大仓库，第一次整仓搜索在5秒内搜不完（Beta Issue #24）；
负责人2026-11-14选方案A：未配置`workspace_read_timeout_seconds`时Windows默认20秒，与同机
自托管端点的默认相同。其他平台仍为5秒。
"""

from __future__ import annotations

import os
from typing import Final

from harnessix.product_config.contracts import ProductConfigV2
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS

WINDOWS_READ_TIMEOUT_SECONDS: Final = 20


def workspace_read_timeout(config: ProductConfigV2, *, windows: bool | None = None) -> int:
    """配置的读取时限；未配置时Windows为20秒，其他平台为5秒。"""

    if config.workspace_read_timeout_seconds is not None:
        return config.workspace_read_timeout_seconds
    on_windows = os.name == "nt" if windows is None else windows
    return WINDOWS_READ_TIMEOUT_SECONDS if on_windows else READ_TIMEOUT_SECONDS
