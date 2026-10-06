"""原Artifact页裁切与审批读取预算；生产者预检与消费者共用相同边界。"""

from __future__ import annotations

from typing import Final

MAX_ARTIFACT_BYTES: Final = 1024 * 1024
MAX_ARTIFACT_RECORDS: Final = 10000
MAX_PAGE_BYTES: Final = 24 * 1024

ARTIFACT_PAGE_LIMIT: Final = 200
MAX_ARTIFACT_PAGES: Final = 50
ARTIFACT_READ_TIMEOUT_SECONDS: Final = 5.0


def paginate_artifact_lines(lines: list[str], offset: int, limit: int) -> tuple[str, int]:
    """已校验记录按原条数及24KiB双重裁切，返回正文与排他的终止偏移。"""
    selected, size = [], 0
    for line in lines[offset : offset + limit]:
        encoded = len(line.encode()) + 1
        if size + encoded > MAX_PAGE_BYTES:
            break
        selected.append(line + "\n")
        size += encoded
    return "".join(selected), offset + len(selected)
