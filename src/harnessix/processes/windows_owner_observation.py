"""Windows Owner的pipe观察支持；二进制读取、进度与启动失败回执保持独立。"""

from __future__ import annotations

import os
import sys

from harnessix.processes.owner_output import (
    launch_failed_output_receipt as launch_failed_output_receipt,
)
from harnessix.processes.owner_output import (
    output_position as output_position,
)


def configure_binary_output_reader(terminal: str, name: str, descriptor: int) -> None:
    """只调整原生pipe的输出FD，避免CRT换行/Ctrl-Z转换，不修改控制或ConPTY。"""
    if sys.platform == "win32" and terminal == "pipe" and name in {"stdout", "stderr"}:
        import msvcrt

        msvcrt.setmode(descriptor, os.O_BINARY)
