"""CAS 控制来源端口：标记实际观察回调，不以 Blob 错误码猜测来源。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from harnessix.workspace.native_observation_io import UpstreamCheckpointError

type BlobErrorMarker = Callable[[BaseException], UpstreamCheckpointError]
type ControlledBlobRead = Callable[[str, BlobErrorMarker], bytes]


@dataclass(frozen=True, slots=True)
class WorkspaceBlobReader:
    """原读取与显式控制读取成对装配；不持有写入、认证或迁移权限。"""

    original: Callable[[str], bytes]
    controlled: ControlledBlobRead

    def __call__(self, digest: str) -> bytes:
        return self.original(digest)


def read_workspace_blob(
    reader: Callable[[str], bytes], digest: str, mark_error: BlobErrorMarker
) -> bytes:
    """仅消费明确装配的端口；普通 Callable 保留原调用方式。"""
    if type(reader) is WorkspaceBlobReader:
        return reader.controlled(digest, mark_error)
    return reader(digest)


@contextmanager
def workspace_blob_read_boundary(
    reader: Callable[[str], bytes],
) -> Iterator[Callable[[str], bytes]]:
    """穿过坏数据分类后，只解本次消费标记；外来嵌套异常原样保留。"""
    if type(reader) is not WorkspaceBlobReader:
        yield reader
        return
    owned_errors: list[UpstreamCheckpointError] = []

    def mark(error: BaseException) -> UpstreamCheckpointError:
        marked = UpstreamCheckpointError(error)
        owned_errors.append(marked)
        return marked

    try:
        yield lambda digest: read_workspace_blob(reader, digest, mark)
    except UpstreamCheckpointError as error:
        if not any(error is marked for marked in owned_errors):
            raise
        raise error.error from None
