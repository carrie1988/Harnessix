"""从上下文与字符串工具提炼的离线评测函数。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import Any


def stringify_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n" + stringify_dict(value)
    if isinstance(value, list):
        return "\n".join(stringify_value(item) for item in value)
    return str(value)


def stringify_dict(data: dict[Any, Any]) -> str:
    text = ""
    for key, value in data.items():
        text += key + ": " + stringify_value(value) + "\n"
    return text


def sanitize_for_storage(text: str, replacement: str = "") -> str:
    return text.replace("\x00", "")


def batch_iterate[T](size: int | None, iterable: Iterable[T]) -> Iterator[list[T]]:
    iterator = iter(iterable)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk
