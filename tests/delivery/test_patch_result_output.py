"""Workspace Patch v2成功结果：发布后摘要可直接作下一次expected_sha256，Diff预览有界。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.transaction_action_executor import _diff_preview
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.execution.public_tool_contracts import (
    MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES,
    WORKSPACE_PATCH_DIFF_PREVIEW_MARKER,
    PublicWorkspacePatchOutputV2,
)
from harnessix.patches.contracts import ExactEdit
from harnessix.tools.runtime import CodingToolRuntime
from tests.delivery.test_trusted_action_patch import _planned_action
from tests.tools.test_files import execute

LIMIT = MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES
MARKER = WORKSPACE_PATCH_DIFF_PREVIEW_MARKER


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def checked(output: object) -> PublicWorkspacePatchOutputV2:
    return PublicWorkspacePatchOutputV2.model_validate_json(json.dumps(output, ensure_ascii=False))


def three_files(root: Path) -> WorkspacePatchInput:
    (root / "keep.py").write_bytes(b"a = 1\nb = 2\n")
    (root / "gone.txt").write_bytes(b"remove\n")
    return WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="edit",
                path="keep.py",
                expected_sha256=sha(b"a = 1\nb = 2\n"),
                edits=(ExactEdit(old_text="b = 2", new_text="b = 3"),),
            ),
            WorkspacePatchFile(operation="create", path="new.py", content="x = 1\n", mode=0o644),
            WorkspacePatchFile(
                operation="delete", path="gone.txt", expected_sha256=sha(b"remove\n")
            ),
        )
    )


async def test_success_reports_published_digests_not_preimages_and_the_whole_small_diff(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    proposal = three_files(root)
    router, executor, planner, plans, audit, transactions, leases, route = _planned_action(
        root, tmp_path / "state", proposal
    )
    try:
        plan_id = route.plan.execution.plan_id
        outcome = await router.execute(plan_id)
        assert outcome.kind == "succeeded"
        output = checked(outcome.output)
        assert (output.spec_version, output.origin, output.files) == (
            "harnessix.workspace-patch-output/v2",
            "execution",
            3,
        )
        record = transactions.load(plan_id)
        assert [change.path for change in output.changes] == [
            mutation.path for mutation in record.plan.mutations
        ]
        digests = {change.path: change.content_sha256 for change in output.changes}
        assert digests == {
            "keep.py": sha((root / "keep.py").read_bytes()),
            "new.py": sha((root / "new.py").read_bytes()),
            "gone.txt": None,
        }
        assert digests["keep.py"] == sha(b"a = 1\nb = 3\n") != sha(b"a = 1\nb = 2\n")
        # 与read_file完整读取的content_sha256是同一摘要。
        async with CodingToolRuntime(root) as tools:
            read = await execute(tools, path="keep.py")
        assert read.output["digest_status"] == "complete"
        assert read.output["content_sha256"] == digests["keep.py"]
        diff = build_workspace_diff(record.plan, transactions)
        assert output.diff_sha256 == diff.sha256
        assert (output.diff_preview.text, output.diff_preview.total_bytes) == (
            diff.text,
            diff.utf8_bytes,
        )
        assert not output.diff_preview.truncated

        recovered = await executor.reconcile(route.plan, proposal)
        again = checked(recovered.output)
        assert again.origin == "recovery"
        assert again.model_dump(exclude={"origin"}) == output.model_dump(exclude={"origin"})
    finally:
        for store in (transactions, leases, plans, audit):
            store.close()


async def test_returned_digest_is_accepted_as_the_next_precondition_without_a_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    proposal = three_files(root)
    first = _planned_action(root, tmp_path / "first", proposal)
    second = None
    try:
        outcome = await first[0].execute(first[-1].plan.execution.plan_id)
        digests = {change.path: change.content_sha256 for change in checked(outcome.output).changes}
        follow_up = WorkspacePatchInput(
            files=(
                WorkspacePatchFile(
                    operation="edit",
                    path="keep.py",
                    expected_sha256=digests["keep.py"],
                    edits=(ExactEdit(old_text="b = 3", new_text="b = 4"),),
                ),
                WorkspacePatchFile(
                    operation="replace",
                    path="new.py",
                    expected_sha256=digests["new.py"],
                    content="x = 2\n",
                    mode=0o644,
                ),
            )
        )
        second = _planned_action(root, tmp_path / "second", follow_up)
        result = await second[0].execute(second[-1].plan.execution.plan_id)
        assert result.kind == "succeeded"
        assert (root / "keep.py").read_bytes() == b"a = 1\nb = 4\n"
        assert (root / "new.py").read_bytes() == b"x = 2\n"
    finally:
        for prepared in (first, second):
            if prepared is not None:
                _, _, _, plans, audit, transactions, leases, _ = prepared
                for store in (transactions, leases, plans, audit):
                    store.close()


async def test_large_diff_preview_is_cut_at_a_line_end_with_the_explicit_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    before = "".join(f"value_{index} = {index}\n" for index in range(400)).encode()
    (root / "big.py").write_bytes(before)
    proposal = WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="replace",
                path="big.py",
                expected_sha256=sha(before),
                content=before.decode().replace(" = ", " == "),
                mode=0o644,
            ),
        )
    )
    router, _, _, plans, audit, transactions, leases, route = _planned_action(
        root, tmp_path / "state", proposal
    )
    try:
        outcome = await router.execute(route.plan.execution.plan_id)
        preview = checked(outcome.output).diff_preview
        diff = build_workspace_diff(
            transactions.load(route.plan.execution.plan_id).plan, transactions
        )
        assert diff.utf8_bytes > 3 * LIMIT
        assert preview.truncated and preview.total_bytes == diff.utf8_bytes
        shown = preview.text.removesuffix(MARKER)
        assert preview.text.endswith(MARKER) and shown.endswith("\n")
        assert diff.text.startswith(shown)
        assert LIMIT - 64 < len(preview.text.encode()) <= LIMIT
    finally:
        for store in (transactions, leases, plans, audit):
            store.close()


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_preview_without_a_line_end_keeps_whole_utf8_characters(prefix: int) -> None:
    text = "x" * prefix + "中" * LIMIT
    total = len(text.encode())
    preview = _diff_preview(text, total)
    shown = preview["text"].removesuffix(MARKER)
    assert preview["truncated"] and preview["total_bytes"] == total
    assert text.startswith(shown) and "�" not in shown
    assert LIMIT - 2 - len(MARKER.encode()) <= len(shown.encode())
    assert len(preview["text"].encode()) <= LIMIT


@pytest.mark.parametrize("size", [1, LIMIT - 1, LIMIT])
def test_diff_at_or_below_the_limit_is_returned_whole(size: int) -> None:
    text = "y" * (size - 1) + "\n"
    assert _diff_preview(text, size) == {"text": text, "total_bytes": size, "truncated": False}
