"""Workspace与Git共享唯一完整Diff编码；内容不携带事务身份、批准或业务认证。"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from io import StringIO

from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES as MAX_DIFF_CONTENT_BYTES
from harnessix.delivery.contracts import DiffKind, WorkspaceDiffEntry, WorkspaceMutation


class DiffContentLimitError(ValueError):
    """完整输出超出声明上限时整体拒绝，不携带正文或路径。"""

    def __init__(self) -> None:
        super().__init__("完整Diff超过声明容量")


@dataclass(frozen=True, slots=True)
class WorkspaceDiffContent:
    """仅保存完整审阅内容；调用者仍须绑定自己的来源、版本和新批准。"""

    entries: tuple[WorkspaceDiffEntry, ...] = field(repr=False)
    text: str = field(repr=False)
    utf8_bytes: int
    sha256: str


@dataclass(slots=True)
class _Output:
    """逐片完整UTF-8记账；None仅用于旧门面保留原Document最终校验。"""

    limit: int | None
    stream: StringIO = field(default_factory=StringIO, repr=False)
    size: int = 0

    def append(self, chunk: str) -> None:
        size = len(chunk.encode("utf-8", errors="strict"))
        if self.limit is not None and self.size + size > self.limit:
            raise DiffContentLimitError()
        # 流式累积完整正文，避免把全部逐行小字符串长期保存在列表中。
        self.stream.write(chunk)
        self.size += size


def build_diff_content(
    mutations: tuple[WorkspaceMutation, ...],
    read_blob: Callable[[str], bytes],
    *,
    max_utf8_bytes: int | None,
    checkpoint: Callable[[], None],
    missing_newline_markers: bool = False,
) -> WorkspaceDiffContent:
    """消费宿主已核验的Mutation和只读正文端口；不替代路径、归属或CAS核验。"""

    if (
        (
            max_utf8_bytes is not None
            and (
                type(max_utf8_bytes) is not int or not 1 <= max_utf8_bytes <= MAX_DIFF_CONTENT_BYTES
            )
        )
        or type(missing_newline_markers) is not bool
        or not callable(read_blob)
        or not callable(checkpoint)
    ):
        raise ValueError("完整Diff参数无效")
    checkpoint()
    rename_pairs = _rename_pairs(mutations, checkpoint)
    renamed_paths = {item.path for pair in rename_pairs for item in pair}
    entries: list[WorkspaceDiffEntry] = []
    output = _Output(max_utf8_bytes)
    for deleted, added in rename_pairs:
        checkpoint()
        body = _blob(read_blob, deleted.before.sha256)
        checkpoint()
        binary = _text(body) is None
        entries.append(
            WorkspaceDiffEntry(
                kind="renamed",
                path=added.path,
                original_path=deleted.path,
                before=deleted.before,
                after=added.after,
                binary=binary,
            )
        )
        output.append(
            f"diff --harnessix a/{deleted.path} b/{added.path}\n"
            f"similarity index 100%\nrename from {deleted.path}\nrename to {added.path}\n"
        )
    for mutation in mutations:
        checkpoint()
        if mutation.path in renamed_paths:
            continue
        entries.append(
            _append_change(mutation, read_blob, output, checkpoint, missing_newline_markers)
        )
    entries.sort(key=lambda item: (item.original_path or item.path, item.path))
    checkpoint()
    text = output.stream.getvalue()
    body = text.encode("utf-8", errors="strict")
    digest = hashlib.sha256(body).hexdigest()
    checkpoint()
    return WorkspaceDiffContent(tuple(entries), text, len(body), digest)


def _rename_pairs(
    mutations: tuple[WorkspaceMutation, ...], checkpoint: Callable[[], None]
) -> list[tuple[WorkspaceMutation, WorkspaceMutation]]:
    """保留原唯一候选与已用目标规则，不把相同内容猜测为新的业务授权。"""
    deletions = [item for item in mutations if item.after.presence == "absent"]
    additions = [item for item in mutations if item.before.presence == "absent"]
    result = []
    used: set[str] = set()
    for deleted in deletions:
        checkpoint()
        candidates = []
        for added in additions:
            checkpoint()
            if added.path not in used and deleted.before == added.after:
                candidates.append(added)
        if len(candidates) == 1:
            result.append((deleted, candidates[0]))
            used.add(candidates[0].path)
    return result


def _append_change(
    mutation: WorkspaceMutation,
    read_blob: Callable[[str], bytes],
    output: _Output,
    checkpoint: Callable[[], None],
    markers: bool,
) -> WorkspaceDiffEntry:
    """完整读取单项前后版本，逐片写入同一预算，再返回结构化条目。"""
    before = _blob(read_blob, mutation.before.sha256)
    checkpoint()
    after = _blob(read_blob, mutation.after.sha256)
    checkpoint()
    before_text, after_text = _text(before), _text(after)
    binary = before_text is None or after_text is None
    kind: DiffKind = (
        "added"
        if mutation.before.presence == "absent"
        else "deleted"
        if mutation.after.presence == "absent"
        else "modified"
    )
    entry = WorkspaceDiffEntry(
        kind=kind,
        path=mutation.path,
        before=mutation.before,
        after=mutation.after,
        binary=binary,
    )
    for chunk in _section(mutation, before_text, after_text, markers):
        checkpoint()
        output.append(chunk)
    return entry


def _blob(reader: Callable[[str], bytes], digest: str | None) -> bytes:
    return b"" if digest is None else reader(digest)


def _text(body: bytes) -> str | None:
    if b"\0" in body:
        return None
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeError:
        return None


def _lines(text: str, lf_only: bool) -> list[str]:
    """旧表示保留原分行；Git表示仅以LF分行，不误把CR或Unicode分隔符当EOF。"""
    if not lf_only:
        return text.splitlines(keepends=True)
    with StringIO(text, newline="\n") as reader:
        return reader.readlines()


def _section(
    mutation: WorkspaceMutation,
    before: str | None,
    after: str | None,
    missing_newline_markers: bool,
) -> Iterator[str]:
    yield f"diff --harnessix a/{mutation.path} b/{mutation.path}\n"
    if mutation.before.mode != mutation.after.mode:
        if mutation.before.mode is None:
            yield f"new file mode {mutation.after.mode:o}\n"
        elif mutation.after.mode is None:
            yield f"deleted file mode {mutation.before.mode:o}\n"
        else:
            yield f"old mode {mutation.before.mode:o}\nnew mode {mutation.after.mode:o}\n"
    if before is None or after is None:
        yield (
            f"Binary files differ: before={mutation.before.sha256 or 'absent'} "
            f"after={mutation.after.sha256 or 'absent'}\n"
        )
        return
    from_name = "/dev/null" if mutation.before.presence == "absent" else f"a/{mutation.path}"
    to_name = "/dev/null" if mutation.after.presence == "absent" else f"b/{mutation.path}"
    for chunk in difflib.unified_diff(
        _lines(before, missing_newline_markers),
        _lines(after, missing_newline_markers),
        fromfile=from_name,
        tofile=to_name,
        lineterm="\n",
    ):
        yield chunk
        if missing_newline_markers and not chunk.endswith("\n"):
            yield "\n\\ No newline at end of file\n"
