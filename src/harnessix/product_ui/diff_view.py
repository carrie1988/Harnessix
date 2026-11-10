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
    return header + _diff_lines(body.splitlines())


def _diff_lines(lines: list[str]) -> list[Text]:
    """每个文件以“── 路径 ──”开头；文件头里的diff/---/+++行与文件清单重复，不再显示。"""

    result: list[Text] = []
    in_header = False
    for line in lines:
        if line.startswith("diff "):
            result.append(Text(f"── {_diff_path(line)} ──", style="bold"))
            in_header = True
        elif in_header and line.startswith(("--- ", "+++ ", "index ", "new file", "deleted file")):
            continue
        else:
            in_header = in_header and not line.startswith("@@")
            result.append(_styled(line))
    return result


def _diff_path(line: str) -> str:
    parts = line.split()
    if len(parts) < 2:
        return line
    old, new = parts[-2].removeprefix("a/"), parts[-1].removeprefix("b/")
    return new if old == new else f"{old} → {new}"


def _styled(line: str) -> Text:
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    return Text(line)
