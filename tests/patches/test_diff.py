import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch, verify_patch_batch
from harnessix.patches.contracts import ExactEdit
from harnessix.patches.diff import patch_batch_diff
from harnessix.patches.diff_contracts import DiffText, PatchBatchDiff, PatchDiffOptions
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.patches.test_batches import group as group
from tests.patches.test_planner import proposal


@pytest.mark.parametrize("prefix", ["", "中", "\ufeff", "e\u0301", "行一\r\n"])
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_byte_offsets_reconstruct_full_target(tmp_path, prefix, ending):
    source = (prefix + "FIRST 中 LAST" + ending).encode()
    (tmp_path / "main.py").write_bytes(source)
    with Workspace(tmp_path) as workspace:
        edits = (
            ExactEdit(old_text="LAST", new_text=""),
            ExactEdit(old_text="FIRST", new_text="替换更长"),
        )
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(files=(proposal(workspace, edits=edits),)),
            ReadOperation(),
        )
        report = patch_batch_diff(workspace, batch, ReadOperation())
        assert PatchBatchDiff.model_validate_json(report.model_dump_json()) == report
        assert not report.truncated and report.total_files == 1 and report.total_edits == 2
        changes = report.edits
        assert changes[0].before_start == len(prefix.encode())
        assert changes[0].edit_index == 0 and changes[1].edit_index == 1
        rebuilt = source
        for change in reversed(changes):
            old, new = change.before.text.encode(), change.after.text.encode()
            assert source[change.before_start : change.before_start + len(old)] == old
            rebuilt = (
                rebuilt[: change.before_start] + new + rebuilt[change.before_start + len(old) :]
            )
        assert rebuilt == batch.patches[0].after
        for change in changes:
            start, body = change.after_start, change.after.text.encode()
            assert rebuilt[start : start + len(body)] == body
        assert changes[1].after.total_bytes == 0 and not changes[1].after.truncated
    assert (tmp_path / "main.py").read_bytes() == source


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4, 1024, 4096])
def test_preview_never_splits_utf8_and_reports_complete_hash(tmp_path, limit):
    old, new = "旧🙂\r\n" * 1000, "新值\r\n" * 1000
    (tmp_path / "main.py").write_text(old, encoding="utf-8")
    with Workspace(tmp_path) as workspace:
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(files=(proposal(workspace, old=old, new=new),)),
            ReadOperation(),
        )
        report = patch_batch_diff(
            workspace, batch, ReadOperation(), PatchDiffOptions(preview_bytes=limit)
        )
        assert len(report.edits) == 1 and report.truncated
        for field, text in ((report.edits[0].before, old), (report.edits[0].after, new)):
            body = text.encode()
            assert field.truncated
            assert len(field.text.encode()) <= limit and body.startswith(field.text.encode())
            assert (
                field.total_bytes == len(body) and field.sha256 == hashlib.sha256(body).hexdigest()
            )
        assert PatchBatchDiff.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("budget", [256, 512, 800, 1200, 2048, 65536])
def test_actual_json_budget_is_a_prefix_not_silent_skips(group, budget):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    full = patch_batch_diff(workspace, batch, ReadOperation())
    result = patch_batch_diff(
        workspace, batch, ReadOperation(), PatchDiffOptions(max_output_bytes=budget)
    )
    assert len(result.model_dump_json().encode()) <= budget
    assert result.edits == full.edits[: len(result.edits)]
    assert result.truncated == (len(result.edits) < result.total_edits)
    assert (result.total_files, result.total_edits) == (2, 2)
    assert PatchBatchDiff.model_validate_json(result.model_dump_json()) == result


def test_json_escaping_counts_towards_budget(tmp_path):
    old, new = '\t"\\\n' * 900, "替换\r\n" * 900
    (tmp_path / "main.py").write_text(old, encoding="utf-8")
    with Workspace(tmp_path) as workspace:
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(files=(proposal(workspace, old=old, new=new),)),
            ReadOperation(),
        )
        full = patch_batch_diff(
            workspace, batch, ReadOperation(), PatchDiffOptions(preview_bytes=4096)
        )
        exact = len(full.model_dump_json().encode())
        assert exact > 4096
        report = patch_batch_diff(
            workspace,
            batch,
            ReadOperation(),
            PatchDiffOptions(preview_bytes=4096, max_output_bytes=exact),
        )
        assert report == full
        smaller = patch_batch_diff(
            workspace,
            batch,
            ReadOperation(),
            PatchDiffOptions(preview_bytes=4096, max_output_bytes=exact - 1),
        )
        assert not smaller.edits and smaller.truncated


def test_diff_is_plan_observation_not_current_workspace_or_execution(group, tmp_path, monkeypatch):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    (tmp_path / "one.py").write_bytes(b"external")
    with pytest.raises(KernelError):
        verify_patch_batch(workspace, batch, ReadOperation())

    def no_open(*args, **kwargs):
        pytest.fail("Diff 不应读取当前文件或写入工作区")

    monkeypatch.setattr(workspace, "open", no_open)
    report = patch_batch_diff(workspace, batch, ReadOperation())
    assert report.edits[0].before.text == "before" and report.edits[0].after.text == "after"
    assert (tmp_path / "one.py").read_bytes() == b"external"


def test_diff_rejects_corrupt_private_payload(group):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    corrupt = replace(batch, patches=(replace(batch.patches[0], after=b"forged"), batch.patches[1]))
    with pytest.raises(KernelError) as error:
        patch_batch_diff(workspace, corrupt, ReadOperation())
    assert error.value.code == "patch_plan_corrupt"


def test_maximum_edit_count_is_bounded_and_complete_with_large_budget(tmp_path):
    edits = tuple(ExactEdit(old_text=f"OLD-{i:02d}", new_text=f"NEW-{i:02d}") for i in range(32))
    before = "\n".join(e.old_text for e in edits)
    for i in range(16):
        (tmp_path / f"{i}.py").write_text(before)
    with Workspace(tmp_path) as workspace:
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(
                files=tuple(proposal(workspace, path=f"{i}.py", edits=edits) for i in range(16))
            ),
            ReadOperation(),
        )
        report = patch_batch_diff(
            workspace, batch, ReadOperation(), PatchDiffOptions(max_output_bytes=1024 * 1024)
        )
        assert len(report.edits) == 512 and not report.truncated
        assert len(report.model_dump_json().encode()) <= 1024 * 1024
        assert PatchBatchDiff.model_validate_json(report.model_dump_json()) == report
        limited = patch_batch_diff(workspace, batch, ReadOperation())
        assert limited.truncated and 0 < len(limited.edits) < 512
        assert len(limited.model_dump_json().encode()) <= 65536
        assert limited.edits == report.edits[: len(limited.edits)]


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_diff_respects_operation_budget(group, mode):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    operation = ReadOperation()
    if mode == "cancel":
        operation.stopped.set()
    else:
        operation.deadline = 0
    with pytest.raises(TurnCancelled if mode == "cancel" else KernelError):
        patch_batch_diff(workspace, batch, operation)


@pytest.mark.parametrize(
    "update",
    [
        {"max_output_bytes": 255},
        {"max_output_bytes": 1048577},
        {"preview_bytes": -1},
        {"preview_bytes": 4097},
        {"preview_bytes": True},
        {"approved": True},
    ],
)
def test_strict_diff_options(update):
    with pytest.raises(ValidationError):
        PatchDiffOptions.model_validate(update)


@pytest.mark.parametrize(
    "update", [{"truncated": True}, {"sha256": "0" * 64}, {"total_bytes": 1}, {"text": "\x00"}]
)
def test_text_contract_does_not_lie_about_complete_preview(update):
    data = {
        "text": "中文",
        "total_bytes": 6,
        "sha256": hashlib.sha256("中文".encode()).hexdigest(),
        "truncated": False,
        **update,
    }
    with pytest.raises(ValidationError):
        DiffText.model_validate(data)


@pytest.mark.parametrize(
    "change", ["truncated", "missing", "duplicate", "count", "files", "offset", "version"]
)
def test_report_contract_rejects_inconsistent_metadata(group, change):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = patch_batch_diff(workspace, batch, ReadOperation())
    data = report.model_dump(mode="json")
    if change == "truncated":
        data["truncated"] = True
    elif change == "missing":
        data["edits"].pop()
    elif change == "duplicate":
        data["edits"][1] = data["edits"][0]
    elif change == "count":
        data["total_edits"] = 1
    elif change == "files":
        data["total_files"] = 1
    elif change == "offset":
        data["edits"][0]["before_start"] = 1024 * 1024
    else:
        data["version"] = "patch-batch-diff/v2"
    with pytest.raises(ValidationError):
        PatchBatchDiff.model_validate_json(json.dumps(data))
