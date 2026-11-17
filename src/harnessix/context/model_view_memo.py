"""模型视图的逐项结果复用（ADR 0155）：内容没有变化的历史Item不在每个模型步骤重新核对与序列化。

职责：给冻结模型算出严格的内容指纹，在进程内按指纹记住纯函数的结果，并定义表中两种记录的形状。
非职责：不判断结果是否成立、不读Session、不落盘、不保存视图对象；结果依赖的全部输入由调用方放进
键里。失败的计算不进表，下次照常重算并照常失败。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import cast

from pydantic import BaseModel

from harnessix.context.tool_result_contracts import ToolResultViewDecision

# 单份模型历史最多8192项，每项至多两条记录（原始事实、已核对的决定）。
MAX_MEMO_ENTRIES = 16_384

MemoKey = tuple[str | bytes, ...]


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """一个原始历史Item的派生字节，只由Item内容决定。"""

    document_utf8_bytes: int
    canonical_json: bytes


@dataclass(frozen=True, slots=True)
class VerifiedDecision:
    """对结果Item核对通过的决定（核对时重新校验得到的那一份），及视图Item与决定的规范JSON。"""

    decision: ToolResultViewDecision
    view_canonical_json: bytes
    decision_canonical_json: bytes


def content_fingerprint(model: BaseModel) -> bytes | None:
    """冻结模型全部字段的严格序列化摘要；内容无法严格序列化时返回None，调用方不得复用结果。

    摘要取自未经JSON模式转换的字段值：1、1.0与true互不相同，字典保留键序，非有限数字直接拒绝
    （JSON模式会把它们写成null，与真正的null无法区分）。所以指纹相同的两个模型，任何只读取其内容
    的计算都得到相同结果。
    """
    try:
        text = json.dumps(
            model.model_dump(),
            default=str,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode()).digest()
    except (TypeError, ValueError, RecursionError):
        return None


def memo_key(kind: str, *fingerprints: bytes | None) -> MemoKey | None:
    """任一输入没有指纹就没有键：这次计算不查表也不入表。"""
    if any(fingerprint is None for fingerprint in fingerprints):
        return None
    return (kind, *cast(tuple[bytes, ...], fingerprints))


class ModelViewMemo:
    """有界的进程内结果表，超出上限时淘汰最久未用的记录；上限为0时每次都重算。

    表内的值必须不可变，并且只由键所代表的内容决定。
    """

    def __init__(self, max_entries: int = MAX_MEMO_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[MemoKey, object] = OrderedDict()
        # Reducer也可能在工作线程里重放事件；查表与淘汰各自在锁内完成，计算本身不持锁。
        self._lock = Lock()

    def recall[T](self, key: MemoKey | None, compute: Callable[[], T]) -> T:
        """键存在且已有记录时返回记录，否则计算；只有成功的结果入表。"""
        if key is None or self._max_entries == 0:
            return compute()
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return cast(T, self._entries[key])
        value = compute()
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return value

    def __len__(self) -> int:
        return len(self._entries)
