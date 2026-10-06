"""宿主注入的原私有CAS端口；不持有业务Store、批准或迁移权限。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotPorts:
    """完整历史的耐久写入与回读；操作检查点由每次调用单独持有。"""

    write_blob: Callable[[str, bytes], None]
    read_blob: Callable[[str], bytes]
