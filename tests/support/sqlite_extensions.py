"""依赖SQLite可加载扩展的测试：当前Python的sqlite3不支持时跳过。

uv在macOS上安装的独立Python（python-build-standalone）编译时关闭了扩展加载，
连接对象没有enable_load_extension；Linux构建支持，相应测试由Linux CI覆盖。
"""

from __future__ import annotations

import sqlite3

import pytest

requires_sqlite_extensions = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason="此Python的sqlite3不支持加载扩展",
)
