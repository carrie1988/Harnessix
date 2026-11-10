"""全部测试共享的收集规则。"""

from __future__ import annotations

import re

import pytest

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def pytest_make_parametrize_id(config: pytest.Config, val: object, argname: str) -> str | None:
    """UUID文本参数用固定ID。

    部分用例在参数里现生成uuid4；pytest-xdist要求每个worker进程收集到完全相同的用例ID，
    否则整轮拒绝运行。参数值不变，只是ID不再随进程变化。
    """

    if isinstance(val, str) and _UUID_TEXT.fullmatch(val):
        return "uuid"
    return None
