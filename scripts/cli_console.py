"""构建与治理CLI的UTF-8输出边界，不改变宿主环境或产物原始字节。"""

from __future__ import annotations

import sys


def configure_utf8_console() -> None:
    """复用原文档/合同门禁行为；管道及旧Windows代码页统一输出UTF-8。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                # 已关闭或无法重配的宿主流保留原行为，不替换测试/嵌入式捕获流。
                pass
