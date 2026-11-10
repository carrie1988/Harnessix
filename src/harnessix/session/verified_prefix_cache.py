"""进程内已验证的Session链前缀缓存（ADR 0126）：游标读取只校验本次返回的事件。

职责：按Thread保存“第i个事件之后的链哈希与累计字节”，并绑定写入时的Publication Binding。
非职责：不验证MAC、不访问数据库、不落盘；前缀的可信来自调用方刚完成、并已与签名检查点比对的校验。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

MAX_CACHED_THREADS = 16


@dataclass(frozen=True, slots=True)
class VerifiedPrefix:
    """第i个事件之后的链哈希与累计历史字节（i=0为空前缀）。"""

    prefix_sha256: str
    history_bytes: int


class VerifiedPrefixCache:
    """单个Store实例内的已验证前缀；Binding变化即清空，超过上限淘汰最久未用的Thread。"""

    def __init__(self, max_threads: int = MAX_CACHED_THREADS) -> None:
        self._max_threads = max_threads
        self._binding: object | None = None
        self._chains: OrderedDict[UUID, list[VerifiedPrefix]] = OrderedDict()

    def prefix(self, binding: object, thread_id: UUID, sequence: int) -> VerifiedPrefix | None:
        """第sequence个事件之后的已验证前缀；没有记录或Binding已变化时返回None。"""
        self._bind(binding)
        chain = self._chains.get(thread_id)
        if chain is None or not 0 <= sequence < len(chain):
            return None
        self._chains.move_to_end(thread_id)
        return chain[sequence]

    def record(
        self, binding: object, thread_id: UUID, start: int, entries: list[VerifiedPrefix]
    ) -> None:
        """记录从第start个事件之后起连续的已验证前缀；与已有记录不衔接时忽略。"""
        self._bind(binding)
        chain = self._chains.get(thread_id, [])
        if start > 0 and (start >= len(chain) or chain[start] != entries[0]):
            return
        self._chains[thread_id] = chain[:start] + entries
        self._chains.move_to_end(thread_id)
        while len(self._chains) > self._max_threads:
            self._chains.popitem(last=False)

    def forget(self, thread_id: UUID) -> None:
        self._chains.pop(thread_id, None)

    def _bind(self, binding: object) -> None:
        if binding is not self._binding:
            self._binding = binding
            self._chains.clear()
