"""Workspace与Git交付：计算前后Workspace镜像的结构化差异。"""

from __future__ import annotations

import difflib
import hashlib

from harnessix.delivery.contracts import (
    DiffKind,
    WorkspaceDiffDocument,
    WorkspaceDiffEntry,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore


def build_workspace_diff(
    plan: WorkspaceTransactionPlan,
    store: SQLiteWorkspaceTransactionStore,
) -> WorkspaceDiffDocument:
    deletions = [item for item in plan.mutations if item.after.presence == "absent"]
    additions = [item for item in plan.mutations if item.before.presence == "absent"]
    rename_pairs: list[tuple[WorkspaceMutation, WorkspaceMutation]] = []
    used_additions: set[str] = set()
    for deleted in deletions:
        candidates = [
            added
            for added in additions
            if added.path not in used_additions and deleted.before == added.after
        ]
        if len(candidates) == 1:
            rename_pairs.append((deleted, candidates[0]))
            used_additions.add(candidates[0].path)
    renamed_paths = {item.path for pair in rename_pairs for item in pair}
    entries: list[WorkspaceDiffEntry] = []
    sections: list[str] = []
    for deleted, added in rename_pairs:
        body = _blob(store, deleted.before.sha256)
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
        sections.append(
            f"diff --harnessix a/{deleted.path} b/{added.path}\n"
            f"similarity index 100%\nrename from {deleted.path}\nrename to {added.path}\n"
        )
    for mutation in plan.mutations:
        if mutation.path in renamed_paths:
            continue
        before = _blob(store, mutation.before.sha256)
        after = _blob(store, mutation.after.sha256)
        before_text = _text(before)
        after_text = _text(after)
        binary = before_text is None or after_text is None
        if mutation.before.presence == "absent":
            kind: DiffKind = "added"
        elif mutation.after.presence == "absent":
            kind = "deleted"
        else:
            kind = "modified"
        entries.append(
            WorkspaceDiffEntry(
                kind=kind,
                path=mutation.path,
                before=mutation.before,
                after=mutation.after,
                binary=binary,
            )
        )
        sections.append(_section(mutation, before_text, after_text))
    entries.sort(key=lambda item: (item.original_path or item.path, item.path))
    text = "".join(sections)
    body = text.encode("utf-8")
    return WorkspaceDiffDocument(
        transaction_id=plan.transaction_id,
        plan_fingerprint=plan.fingerprint,
        entries=tuple(entries),
        text=text,
        utf8_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _blob(store: SQLiteWorkspaceTransactionStore, digest: str | None) -> bytes:
    return b"" if digest is None else store.blob(digest)


def _text(body: bytes) -> str | None:
    if b"\0" in body:
        return None
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeError:
        return None


def _section(
    mutation: WorkspaceMutation,
    before: str | None,
    after: str | None,
) -> str:
    header = f"diff --harnessix a/{mutation.path} b/{mutation.path}\n"
    modes = ""
    if mutation.before.mode != mutation.after.mode:
        if mutation.before.mode is None:
            modes = f"new file mode {mutation.after.mode:o}\n"
        elif mutation.after.mode is None:
            modes = f"deleted file mode {mutation.before.mode:o}\n"
        else:
            modes = f"old mode {mutation.before.mode:o}\nnew mode {mutation.after.mode:o}\n"
    if before is None or after is None:
        return (
            header
            + modes
            + f"Binary files differ: before={mutation.before.sha256 or 'absent'} "
            + f"after={mutation.after.sha256 or 'absent'}\n"
        )
    from_name = "/dev/null" if mutation.before.presence == "absent" else f"a/{mutation.path}"
    to_name = "/dev/null" if mutation.after.presence == "absent" else f"b/{mutation.path}"
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_name,
            tofile=to_name,
            lineterm="\n",
        )
    )
    return header + modes + patch
