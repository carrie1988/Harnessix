"""工作区读取（读文件、列目录、glob、grep）的时限：配置优先，未配置时按平台给默认值。

Windows第一次读到一个文件要先经Defender扫描：发布会话在Windows 11真机上测得约18毫秒/文件，
读过之后约0.15毫秒。刚克隆或很久没打开的大仓库，第一次整仓搜索在5秒内搜不完（Beta Issue #24）；
负责人2026-11-14选方案A：未配置`workspace_read_timeout_seconds`时Windows有更长的默认值。
先定20秒；真机上新克隆的Dify（16,108个文件）第一次整仓搜索要20.5～22秒，负责人同日改为30秒。
其他平台仍为5秒。
"""

from __future__ import annotations

import os
from typing import Final

from harnessix.product_config.contracts import ProductConfigV2
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS

WINDOWS_READ_TIMEOUT_SECONDS: Final = 30


def workspace_read_timeout(config: ProductConfigV2, *, windows: bool | None = None) -> int:
    """配置的读取时限；未配置时Windows为30秒，其他平台为5秒。"""

    if config.workspace_read_timeout_seconds is not None:
        return config.workspace_read_timeout_seconds
    on_windows = os.name == "nt" if windows is None else windows
    return WINDOWS_READ_TIMEOUT_SECONDS if on_windows else READ_TIMEOUT_SECONDS
