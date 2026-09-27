"""Secret扫描的固定预算、计数与失败关闭合同，不携带输入正文。"""

from __future__ import annotations

import time
from dataclasses import dataclass


class ScanIncompleteError(Exception):
    """未覆盖全部输入时阻断门禁；公开诊断只能使用固定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ScanLimits:
    """本地CI输入上限；不允许通过CLI扩大默认安全边界。"""

    file_bytes: int = 16 * 1024 * 1024
    member_bytes: int = 8 * 1024 * 1024
    expanded_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 256 * 1024 * 1024
    central_directory_bytes: int = 2 * 1024 * 1024
    entries: int = 10000
    archive_entries: int = 4096
    archive_depth: int = 3
    findings: int = 1000
    seconds: float = 60.0


@dataclass
class ScanBudget:
    """同一次扫描共享累计预算；嵌套成员不能各自重置上限。"""

    limits: ScanLimits
    started: float
    bytes_read: int = 0
    files: int = 0
    members: int = 0
    hits: int = 0

    def checkpoint(self) -> None:
        if time.monotonic() - self.started > self.limits.seconds:
            raise ScanIncompleteError("scan_timeout")

    def consume(self, size: int) -> None:
        self.checkpoint()
        self.bytes_read += size
        if self.bytes_read > self.limits.total_bytes:
            raise ScanIncompleteError("scan_byte_limit")

    def entry(self, *, member: bool) -> None:
        self.checkpoint()
        if member:
            self.members += 1
        else:
            self.files += 1
        if self.files + self.members > self.limits.entries:
            raise ScanIncompleteError("scan_entry_limit")

    def hit(self) -> None:
        self.checkpoint()
        self.hits += 1
        if self.hits > self.limits.findings:
            raise ScanIncompleteError("scan_finding_limit")
