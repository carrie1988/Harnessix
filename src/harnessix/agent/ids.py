from secrets import randbits
from time import time_ns
from uuid import UUID


def new_id() -> UUID:
    """RFC 9562 UUIDv7；同毫秒内不保证排序，事件顺序由数据库 sequence 决定。"""
    milliseconds = time_ns() // 1_000_000
    return UUID(
        int=(milliseconds << 80) | (7 << 76) | (randbits(12) << 64) | (2 << 62) | randbits(62)
    )
