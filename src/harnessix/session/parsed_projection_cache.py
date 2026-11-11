"""进程内的Thread投影解析缓存（ADR 0133）：原字节没有变化时不重复解析同一份投影。

职责：按Thread记住最近一份“原字节SHA-256与投影版本→已解析并通过内容校验的Thread”。
非职责：不读数据库、不验证签名或摘要、不判断投影是否可信、不落盘。每次读取仍由调用方取回原行、
重算摘要并完成全部存储侧校验；只有调用方本次算出的摘要与版本都与记录相同时才复用解析结果。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.models import Thread
from harnessix.agent.tool_rejections import require_closed_rejections
from harnessix.session.store_publication import require_projection_content_version

MAX_CACHED_THREADS = 16


@dataclass(frozen=True, slots=True)
class _ParsedProjection:
    snapshot_sha256: str
    projection_version: int
    thread: Thread


class ParsedProjectionCache:
    """单个Store实例内的解析结果；每个Thread只留最近一份，超过上限淘汰最久未用的Thread。

    命中时多个读取方拿到同一个Thread对象：Thread及其内部模型都是冻结模型，读取方不得原地修改其中的
    JSON容器。
    """

    def __init__(self, max_threads: int = MAX_CACHED_THREADS) -> None:
        self._max_threads = max_threads
        self._entries: OrderedDict[UUID, _ParsedProjection] = OrderedDict()

    def parsed(
        self, thread_id: UUID, snapshot_sha256: str, projection_version: int
    ) -> Thread | None:
        """摘要与版本都与最近一份记录相同才返回其Thread。"""
        entry = self._entries.get(thread_id)
        if (
            entry is None
            or entry.snapshot_sha256 != snapshot_sha256
            or entry.projection_version != projection_version
        ):
            return None
        self._entries.move_to_end(thread_id)
        return entry.thread

    def record(
        self, thread_id: UUID, snapshot_sha256: str, projection_version: int, thread: Thread
    ) -> None:
        """记录刚由这份原字节解析并通过内容校验的Thread，替换该Thread的旧记录。"""
        self._entries[thread_id] = _ParsedProjection(snapshot_sha256, projection_version, thread)
        self._entries.move_to_end(thread_id)
        while len(self._entries) > self._max_threads:
            self._entries.popitem(last=False)


def parse_projection(
    cache: ParsedProjectionCache | None,
    thread_id: UUID,
    encoded: str,
    snapshot_sha256: str,
    projection_version: int,
) -> Thread:
    """解析投影并校验版本内容与拒绝闭合；snapshot_sha256必须是调用方本次对encoded原字节算出的摘要。

    解析是原字节的纯函数，摘要相同即结果相同，所以命中缓存时三步都跳过。结构不合法时抛出
    pydantic的ValidationError，由调用方映射为各自的稳定错误码；失败的结果不进缓存。
    """
    if cache is not None:
        cached = cache.parsed(thread_id, snapshot_sha256, projection_version)
        if cached is not None:
            return cached
    thread = Thread.model_validate_json(encoded)
    require_projection_content_version(thread, projection_version)
    require_closed_rejections(thread)
    if cache is not None:
        cache.record(thread_id, snapshot_sha256, projection_version, thread)
    return thread
