"""命令行打印的JSON：输出流不是终端时只写ASCII（非ASCII字符写成\\u转义），终端里照写原文。

重定向或管道里的输出可能被宿主转码：Windows PowerShell 5.1与未开UTF-8模式的控制台按系统代码页
（如cp936）编码，调用方按UTF-8解析时中文成了乱码，引号被吞后JSON无法解析（Beta Issue #26的Windows
复测；`data export`的错误说明同样，0.10.0b53正式测试发现）。转义后解析出的内容不变。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def is_terminal(stream: TextIO) -> bool:
    """输出流是终端；已关闭、没有isatty的流按不是终端处理。"""
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def print_json(value: object, *, file: TextIO | None = None, **options: Any) -> None:
    """按输出流决定是否只写ASCII后打印一行JSON；options原样交给json.dumps。"""
    stream = sys.stdout if file is None else file
    print(json.dumps(value, ensure_ascii=not is_terminal(stream), **options), file=stream)
