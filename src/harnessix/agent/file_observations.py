"""从发给模型的历史中提取各文件最近一次完整内容摘要，供提案阶段替模型补全Patch前置条件。

只读取宿主自己写入的成功工具结果（read_file、grep与Workspace Patch），不读取模型文本或调用参数：
摘要代表“模型最近一次被告知的该文件完整内容”。文件此后若被命令改动，摘要即过期，
Patch规划仍按它核对并照常拒绝。不做I/O，不签发任何授权。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from uuid import UUID

from pydantic import JsonValue

from harnessix.agent.models import Item, ToolCallContent, ToolResultContent

_Digest = tuple[str, str | None]


def observed_file_digests(history: Sequence[Item]) -> dict[str, str]:
    """返回路径到SHA-256的映射；同一路径以最后一次观察为准，删除或摘要缺失时移除。"""
    tools: dict[UUID, str] = {}
    observed: dict[str, str] = {}
    for item in history:
        content = item.content
        if isinstance(content, ToolCallContent):
            tools[content.call_id] = content.tool
        elif isinstance(content, ToolResultContent) and content.outcome == "succeeded":
            for path, sha256 in _result_digests(tools.get(content.call_id), content.output):
                if sha256 is None:
                    observed.pop(path, None)
                else:
                    observed[path] = sha256
    return observed


def _result_digests(tool: str | None, output: JsonValue) -> Iterator[_Digest]:
    if not isinstance(output, dict):
        return
    if tool == "read_file":
        yield from _digest(output)
    elif tool == "grep":
        # 有Artifact时结果是{preview, artifact}；每个命中带同次完整读取的文件摘要。
        preview = output.get("preview", output)
        yield from _digests(preview.get("matches") if isinstance(preview, dict) else None)
    elif tool == "apply_patch_batch":
        yield from _digests(output.get("changes"))


def _digests(records: JsonValue) -> Iterator[_Digest]:
    for record in records if isinstance(records, list) else ():
        yield from _digest(record)


def _digest(record: JsonValue) -> Iterator[_Digest]:
    """只认带content_sha256字段的记录：原字面量grep的命中没有完整摘要，不能当作观察。"""
    if not isinstance(record, dict) or "content_sha256" not in record:
        return
    path, sha256 = record.get("path"), record["content_sha256"]
    if isinstance(path, str) and (sha256 is None or isinstance(sha256, str)):
        yield path, sha256
