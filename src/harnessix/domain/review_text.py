"""已知完整审阅JSONL的原文重建，仅用于公开保护，不签发领域认证。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from harnessix.domain.artifact_pagination import MAX_ARTIFACT_BYTES

_VERSIONS = {
    "harnessix.workspace-action-review/v1",
    "harnessix.product-git-action-review/v1",
}


def review_text_for_protection(body: bytes, *, checkpoint: Callable[[], None]) -> str | None:
    """原行完整性先由Artifact验证；已知Review再核对全文，旧通用格式保持原保护。"""
    checkpoint()
    if type(body) is not bytes or len(body) > MAX_ARTIFACT_BYTES:
        raise ValueError("审阅正文超限")
    lines = body.decode("utf-8", "strict").split("\n")
    if not lines[0]:
        return None
    summary = json.loads(lines[0])
    if (
        type(summary) is not dict
        or type(summary.get("spec_version")) is not str
        or summary["spec_version"] not in _VERSIONS
    ):
        return None
    if not body.endswith(b"\n") or summary.get("record_type") != "summary":
        raise ValueError("完整审阅记录无效")
    entries, size = 0, 0
    chunks: list[str] = []
    digest = hashlib.sha256()
    for line in lines[1:-1]:
        checkpoint()
        record = json.loads(line)
        if type(record) is not dict:
            raise ValueError("完整审阅记录无效")
        if record.get("record_type") == "entry" and not chunks:
            if type(record.get("index")) is not int or record["index"] != entries:
                raise ValueError("完整审阅索引无效")
            entries += 1
        elif record.get("record_type") == "text":
            text = _text_chunk(record, len(chunks))
            encoded = text.encode("utf-8", "strict")
            size += len(encoded)
            if size > MAX_ARTIFACT_BYTES:
                raise ValueError("完整审阅正文超限")
            chunks.append(text)
            digest.update(encoded)
        else:
            raise ValueError("完整审阅顺序无效")
    _complete_summary(summary, entries, bool(chunks), size, digest.hexdigest())
    checkpoint()
    return "".join(chunks)


def _complete_summary(
    summary: dict[str, object], entries: int, has_chunks: bool, size: int, sha256: str
) -> None:
    """完整审阅元数据必须对应重建原文，不能以MAC正确替代当前保护验证。"""
    if (
        not has_chunks
        or summary.get("complete") is not True
        or type(summary.get("file_count")) is not int
        or not 1 <= entries <= 256
        or entries != summary["file_count"]
        or type(summary.get("diff_utf8_bytes")) is not int
        or size != summary["diff_utf8_bytes"]
        or sha256 != summary.get("diff_sha256")
    ):
        raise ValueError("完整审阅全文不一致")


def _text_chunk(record: dict[str, object], sequence: int) -> str:
    """正文必须是连续原字符串；类型转换或省略不能隐藏完整保护材料。"""
    text = record.get("text")
    if (
        type(record.get("sequence")) is not int
        or record["sequence"] != sequence
        or type(text) is not str
        or not 1 <= len(text) <= 3000
    ):
        raise ValueError("完整审阅正文无效")
    return text
