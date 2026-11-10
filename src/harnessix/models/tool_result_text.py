"""模型线协议上的工具结果正文：常见只读与命令结果渲染为紧凑文本，其余保持规范JSON。

持久历史、Artifact、审计与回放仍使用结构化结果；这里只决定发给模型的那份文本，
由结果内容唯一确定。搜索与读取结果里的扫描统计、逐行JSON对象和重复摘要占了大部分字节，
却不帮助模型定位代码；渲染保留模型后续要用的字段名（content_sha256、revision、
next_line、artifact_id），形状不符合预期时一律退回JSON，不猜测字段含义。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

TOOL_RESULT_TEXT_VERSION = "harnessix.tool-result-text/v1"

_PUBLIC_FIELDS = {"outcome", "output", "error", "diff_artifact"}
_GAP_FIELDS = (
    "unreadable_entries",
    "oversized_files",
    "invalid_utf8_files",
    "binary_files",
    "long_lines",
)
_SEARCH_FIELDS = {"path", "stats", "scan_complete", "truncated", "truncation_reason"}
_READ_FIELDS = {
    "path",
    "text",
    "start_line",
    "end_line",
    "utf8_bytes",
    "revision",
    "truncated",
    "truncation_reason",
    "next_line",
}
_READ_SNAPSHOT_FIELDS = _READ_FIELDS | {
    "spec_version",
    "file_bytes",
    "content_sha256",
    "digest_status",
}


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def tool_result_text(public: Mapping[str, Any]) -> str:
    """公开结果字段（outcome/output/error/diff_artifact）对应的模型正文。"""
    rendered = _render(public)
    return encode_json(dict(public)) if rendered is None else rendered


def _render(public: Mapping[str, Any]) -> str | None:
    if (
        set(public) - _PUBLIC_FIELDS
        or public.get("outcome") != "succeeded"
        or public.get("error") is not None
        or public.get("diff_artifact") is not None
    ):
        return None
    output = public.get("output")
    if not isinstance(output, dict):
        return None
    renderers: tuple[Callable[[dict[str, Any]], str | None], ...] = (
        _search,
        _read_file,
        _process,
        _patch,
    )
    for renderer in renderers:
        try:
            text = renderer(output)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if text is not None:
            return text
    return None


def _search(output: dict[str, Any]) -> str | None:
    preview, artifact = output, None
    if set(output) == {"preview", "artifact"} and isinstance(output["preview"], dict):
        preview, artifact = output["preview"], output["artifact"]
    keys = set(preview)
    if not _SEARCH_FIELDS <= keys:
        return None
    extra = keys - _SEARCH_FIELDS
    if extra == {"paths"}:
        return _glob(preview, artifact)
    if extra <= {"matches", "spec_version", "literal_fallback"} and "matches" in extra:
        return _grep(preview, artifact)
    return None


def _scan_note(preview: dict[str, Any], artifact: dict[str, Any] | None) -> str:
    notes = []
    if preview["truncated"]:
        note = f"truncated ({preview['truncation_reason']})"
        if artifact is not None:
            note += f"; full results: read_artifact artifact_id={artifact['artifact_id']}"
        notes.append(note)
    gaps = [f"{preview['stats'][name]} {name}" for name in _GAP_FIELDS if preview["stats"][name]]
    if gaps:
        notes.append("skipped " + ", ".join(gaps))
    if not preview["scan_complete"] and not notes:
        notes.append("scan incomplete")
    return "".join(f"; {note}" for note in notes)


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _glob(preview: dict[str, Any], artifact: dict[str, Any] | None) -> str:
    paths = preview["paths"]
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise TypeError("glob paths")
    count = _count(len(paths), "path", "paths")
    head = f"glob {preview['path']}: {count}{_scan_note(preview, artifact)}"
    return "\n".join([head, *paths])


def _grep(preview: dict[str, Any], artifact: dict[str, Any] | None) -> str:
    matches = preview["matches"]
    if not isinstance(matches, list):
        raise TypeError("grep matches")
    note = _scan_note(preview, artifact)
    if preview.get("literal_fallback"):
        note += "; the regex matched nothing, these are literal matches of the query"
    if not matches:
        searched = preview["stats"]["files_read"]
        return f"grep {preview['path']}: no matches ({searched} files searched){note}"
    lines = [f"grep {preview['path']}: {_count(len(matches), 'match', 'matches')}{note}"]
    files: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        files.setdefault(match["path"], []).append(match)
    for path, hits in files.items():
        first = hits[0]
        header = f"== {path} revision={first['revision']}"
        if "content_sha256" in first:
            header = (
                f"== {path} content_sha256={first['content_sha256']} "
                f"revision={first['revision']} ({first['file_bytes']} bytes)"
            )
        lines.append(header)
        lines.extend(_grep_lines(hits))
    return "\n".join(lines)


def _grep_lines(hits: list[dict[str, Any]]) -> list[str]:
    """同一文件的命中与上下文按行号合并去重；不连续处以“--”分隔（同grep -n）。"""
    numbered: dict[int, tuple[str, bool, bool]] = {}
    for hit in hits:
        context = hit.get("context")
        if context is not None:
            for offset, line in enumerate(context["lines"]):
                number = context["start_line"] + offset
                previous = numbered.get(number)
                is_match = number == hit["line"] or (previous is not None and previous[2])
                numbered[number] = (line["text"], line["text_truncated"], is_match)
        numbered[hit["line"]] = (hit["text"], hit["text_truncated"], True)
    rendered: list[str] = []
    previous_number = None
    for number in sorted(numbered):
        text, truncated, is_match = numbered[number]
        if previous_number is not None and number != previous_number + 1:
            rendered.append("--")
        marker = ":" if is_match else "-"
        rendered.append(f"{number}{marker}{text}{' [line truncated]' if truncated else ''}")
        previous_number = number
    return rendered


def _read_file(output: dict[str, Any]) -> str | None:
    keys = set(output)
    if keys not in (_READ_FIELDS, _READ_SNAPSHOT_FIELDS):
        return None
    if not isinstance(output["text"], str):
        raise TypeError("read_file text")
    if output["end_line"] is None:
        span = f"{output['path']}: no lines from line {output['start_line']}"
    else:
        span = f"{output['path']} lines {output['start_line']}-{output['end_line']}"
    if "file_bytes" in output:
        span += f" of a {output['file_bytes']}-byte file"
    if output["truncated"]:
        span += f"; truncated ({output['truncation_reason']}), next_line={output['next_line']}"
    else:
        span += "; to end of file"
    identity = f"revision={output['revision']}"
    if "digest_status" in output:
        digest = (
            f"content_sha256={output['content_sha256']}"
            if output["digest_status"] == "complete"
            else "content_sha256 omitted (file too large)"
        )
        identity = f"{digest} {identity}"
    return f"{span}\n{identity}\n----\n{output['text']}"


def _process(output: dict[str, Any]) -> str | None:
    version = output.get("version")
    if not isinstance(version, str) or not version.startswith("trusted-process-output/"):
        return None
    preview = output["diagnostic_preview"]
    if not isinstance(preview, dict):
        raise TypeError("process preview")
    if output["state"] == "exited":
        head = f"exit {output['returncode']}"
    else:
        head = f"{output['state']} ({output['stop_reason']})"
    shown = []
    for stream in ("stdout", "stderr"):
        observed = output[stream]["observed_bytes"]
        head += f"; {stream} {observed} bytes"
        stream_preview = preview[stream]
        text = stream_preview["text"]
        if text is None:
            if observed:
                shown.append(f"[{stream}: not shown, binary or control characters]")
            continue
        if not isinstance(text, str):
            raise TypeError("process stream text")
        if not text and not stream_preview["truncated"]:
            continue
        notes = []
        if stream_preview["truncated"]:
            # v3/v4预览是流的末尾（带offset），v2是开头。
            part = "last" if "offset" in stream_preview else "first"
            notes.append(f"{part} {stream_preview['size_bytes']} of {observed} bytes")
        if stream_preview.get("removed_escape_bytes"):
            # v4去除了颜色等终端转义序列；注明正文不是逐字节原样输出。
            notes.append("terminal escape sequences removed")
        label = f"{stream}: {'; '.join(notes)}" if notes else stream
        shown.append(f"[{label}]\n{text}")
    artifact = output.get("artifact")
    truncated = any(preview[stream]["truncated"] for stream in ("stdout", "stderr"))
    if truncated and isinstance(artifact, dict):
        head += f"; full output: read_artifact artifact_id={artifact['artifact_id']}"
    if output.get("complete") is False:
        head += "; output incomplete"
    return "\n".join([head, *shown])


def _patch(output: dict[str, Any]) -> str | None:
    if output.get("spec_version") != "harnessix.workspace-patch-output/v2":
        return None
    lines = [
        f"patch {output['state']}: {_count(output['files'], 'file', 'files')}; "
        f"transaction_id={output['transaction_id']}"
    ]
    for change in output["changes"]:
        digest = change["content_sha256"]
        state = "deleted" if digest is None else f"content_sha256={digest}"
        lines.append(f"{change['path']} {state}")
    preview = output["diff_preview"]
    if preview["truncated"]:
        lines.append(f"[diff: first part of {preview['total_bytes']} bytes]")
    else:
        lines.append("[diff]")
    text: str = preview["text"]
    return "\n".join(lines) + "\n" + text
