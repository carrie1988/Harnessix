"""把审批差异Artifact（逐行JSON记录）整理成可以直接核对的文件清单和着色统一diff。"""

from __future__ import annotations

import json

from rich.text import Text

_KIND_TEXT = {"added": "新增", "modified": "修改", "deleted": "删除", "renamed": "重命名"}


def readable_diff(artifact_text: str) -> list[Text] | None:
    """返回逐行渲染结果；不是预期的记录格式时返回None，由调用方按原文显示。"""

    files: list[str] = []
    chunks: list[tuple[int, str]] = []
    try:
        for line in artifact_text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record["record_type"] == "entry":
                entry = record["entry"]
                files.append(f"  {_KIND_TEXT.get(entry['kind'], entry['kind'])}  {entry['path']}")
            elif record["record_type"] == "text":
                chunks.append((int(record["sequence"]), str(record["text"])))
    except (ValueError, KeyError, TypeError):
        return None
    if not files and not chunks:
        return None
    body = "".join(text for _, text in sorted(chunks))
    header = [Text(f"共 {len(files)} 个文件：", style="bold"), *map(Text, files), Text("")]
    return header + [_styled(line) for line in body.splitlines()]


def _styled(line: str) -> Text:
    if line.startswith(("+++", "---", "diff ")):
        return Text(line, style="bold")
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    return Text(line)
