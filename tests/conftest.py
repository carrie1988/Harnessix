"""全部测试共享的收集规则与临时目录约定。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


_MAX_ID_CHARS = 200


def pytest_make_parametrize_id(config: pytest.Config, val: object, argname: str) -> str | None:
    """UUID文本与超长文本参数用固定ID；参数值不变。

    - 部分用例在参数里现生成uuid4；pytest-xdist要求每个worker进程收集到完全相同的用例ID，
      否则整轮拒绝运行。
    - pytest把用例ID写入环境变量PYTEST_CURRENT_TEST，Windows的环境变量上限是32767字符；
      边界用例的参数常有几万到几百万字符，直接当ID会让用例在setup阶段报错。
    """

    if isinstance(val, str) and _UUID_TEXT.fullmatch(val):
        return "uuid"
    if isinstance(val, str | bytes) and len(val) > _MAX_ID_CHARS:
        return f"{argname}-{len(val)}-{'bytes' if isinstance(val, bytes) else 'chars'}"
    return None


@pytest.fixture(scope="session", autouse=True)
def _private_windows_tmp(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Windows上把测试临时目录收紧为仅当前用户与SYSTEM可访问，并向下继承。

    POSIX上pytest的临时目录本来就是0700；Windows上它继承父目录的权限（含Administrators等），
    而产品只接受“当前用户+SYSTEM”的状态目录，且从不改写已有目录的权限，
    用例里普通mkdir出来的状态目录会被全部拒绝。
    """

    if os.name == "nt":
        _restrict_to_user_and_system(tmp_path_factory.getbasetemp())


def _restrict_to_user_and_system(directory: Path) -> None:
    listing = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    )
    user_sid = listing.stdout.strip().rsplit(",", 1)[-1].strip('"')
    subprocess.run(
        [
            "icacls",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            f"*{user_sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
        ],
        capture_output=True,
        check=True,
    )
