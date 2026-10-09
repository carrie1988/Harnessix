"""使用标准 setuptools 编译器构建内部 CPython/POSIX 候选扩展。"""

import os
import sys

from setuptools import Extension, setup

if sys.platform == "win32" or os.name != "posix":
    raise RuntimeError(
        "harnessix-sqlite-identity is a POSIX-only internal candidate; "
        "Windows builds are blocked pending native identity safety validation."
    )

if sys.implementation.name != "cpython" or sys.version_info < (3, 12):
    raise RuntimeError("harnessix-sqlite-identity requires CPython >= 3.12 (non-abi3).")

setup(
    ext_modules=[
        Extension(
            name="harnessix_sqlite_identity._bridge",
            sources=["src/harnessix_sqlite_identity/bridge.c"],
            include_dirs=["third_party/sqlite"],
            depends=[
                "src/harnessix_sqlite_identity/file_identity_backend.h",
                "third_party/sqlite/sqlite3.h",
                "third_party/sqlite/sqlite3ext.h",
            ],
            extra_compile_args=["-std=c11", "-Wall", "-Wextra"],
            # Linux 的 dladdr 需要 libdl；SQLite 仅使用头文件，不链接第二份实现。
            libraries=["dl"] if sys.platform.startswith("linux") else [],
            py_limited_api=False,
            optional=False,
        ),
    ],
)
