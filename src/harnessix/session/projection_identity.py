"""Thread投影的严格同一性（ADR 0168 D1）：写入方的Thread与解析其提交原字节得到的Thread是否完全相同。

职责：逐层比较类型与取值，供写入方预填解析缓存时的抽样核对与往返测试共用。
非职责：不解析、不读数据库、不判断投影是否可信。
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel


def identical(left: object, right: object) -> bool:
    """两侧逐层类型相同、取值相等，模型字段与字典键的顺序也相同。

    比==更严：StrEnum与str、True与1、列表与元组、-0.0与0.0、不同的时区对象都算不同，因为它们在后续
    处理或重新序列化时可能表现不同。模型“显式赋值过的字段”集合不比较：它不进入默认序列化，产品代码
    也不读取。用显式栈遍历，不受嵌套深度的递归上限影响。
    """
    pending: list[tuple[object, object]] = [(left, right)]
    while pending:
        a, b = pending.pop()
        if type(a) is not type(b):
            return False
        if isinstance(a, BaseModel):
            assert isinstance(b, BaseModel)
            pending.append((a.__dict__, b.__dict__))
            pending.append((a.__pydantic_extra__, b.__pydantic_extra__))
            pending.append((a.__pydantic_private__, b.__pydantic_private__))
        elif isinstance(a, dict):
            assert isinstance(b, dict)
            if len(a) != len(b):
                return False
            for (key_a, value_a), (key_b, value_b) in zip(a.items(), b.items(), strict=True):
                pending.append((key_a, key_b))
                pending.append((value_a, value_b))
        elif isinstance(a, list | tuple):
            assert isinstance(b, list | tuple)
            if len(a) != len(b):
                return False
            pending.extend(zip(a, b, strict=True))
        elif not _same_leaf(a, b):
            return False
    return True


def _same_leaf(a: object, b: object) -> bool:
    """同类型的叶子值：浮点数连符号与NaN一起比，带时区的时间连时区对象的类型与偏移一起比。"""
    if isinstance(a, float):
        assert isinstance(b, float)
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    if isinstance(a, datetime):
        assert isinstance(b, datetime)
        return (
            a == b
            and type(a.tzinfo) is type(b.tzinfo)
            and a.utcoffset() == b.utcoffset()
            and a.isoformat() == b.isoformat()
        )
    return a == b
