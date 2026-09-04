import json
from dataclasses import replace

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_PAGE_BYTES
from harnessix.artifacts.sqlite import records
from harnessix.patches.batch_bridge_contracts import BatchFileOutput, ManagedPatchBatchOutput
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batch_run_contracts import member_effect
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.contracts import ExactEdit
from harnessix.patches.diff import patch_batch_diff
from harnessix.patches.diff_contracts import PatchDiffOptions
from harnessix.patches.diff_document import batch_diff_document
from harnessix.patches.diff_document_contracts import (
    MAX_DOCUMENT_BYTES,
    MAX_RECORD_BYTES,
    BatchDiffDocument,
    BatchDiffDocumentOptions,
    BatchDiffRecord,
)
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.patches.test_batches import group as group
from tests.patches.test_planner import proposal


def check(report):
    body = report.to_jsonl()
    assert MAX_RECORD_BYTES == MAX_PAGE_BYTES and MAX_DOCUMENT_BYTES == MAX_ARTIFACT_BYTES
    assert len(body) <= MAX_ARTIFACT_BYTES
    lines = records(body)
    assert len(lines) == 1 + len(report.files) + len(report.edits)
    adapter = TypeAdapter(BatchDiffRecord)
    values = [adapter.validate_json(line) for line in lines]
    assert tuple(values) == (report.summary, *report.files, *report.edits)
    assert BatchDiffDocument.model_validate_json(report.model_dump_json()) == report
    return body


def output(batch, states, *, phase="finished", reason="failed"):
    effects = {member_effect(s) for s in states}
    aggregate = (
        "unknown"
        if "unknown" in effects
        else "applied"
        if effects == {"applied"}
        else "partial"
        if "applied" in effects
        else "not_applied"
    )
    return ManagedPatchBatchOutput(
        phase=phase,
        stop_reason=reason,
        effect=aggregate,
        files=tuple(
            BatchFileOutput(
                path=m.path,
                state=s,
                effect=member_effect(s),
                before_sha256=m.before_sha256,
                after_sha256=m.after_sha256,
            )
            for s, m in zip(states, batch.manifest.files, strict=True)
        ),
    )


def test_plan_matches_existing_diff_and_is_valid_jsonl(group):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = batch_diff_document(workspace, batch, ReadOperation())
    old = patch_batch_diff(workspace, batch, ReadOperation())
    assert report.summary.view == "plan" and report.summary.complete
    assert report.summary.phase is None and report.summary.effect is None
    assert [e.model_dump(exclude={"kind"}) for e in report.edits] == [
        e.model_dump() for e in old.edits
    ]
    check(report)


@pytest.mark.parametrize(
    "states",
    [
        ("applied", "applied"),
        ("observed_after", "observed_after"),
        ("applied", "failed"),
        ("observed_after", "observed_before"),
        ("applied", "uncertain"),
        ("applied", "diverged"),
        ("applied", "missing"),
        ("applied", "unavailable"),
        ("pending", "pending"),
        ("approved", "pending"),
        ("started", "pending"),
    ],
)
def test_history_only_renders_attributed_members_and_keeps_all_file_rows(group, states):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = batch_diff_document(workspace, batch, ReadOperation(), output=output(batch, states))
    expected = tuple(
        p.manifest.path
        for p, s in zip(batch.patches, states, strict=True)
        if member_effect(s) == "applied"
    )
    assert tuple(e.path for e in report.edits) == expected
    assert tuple(f.state for f in report.files) == states
    assert report.summary.eligible_files == len(expected) and report.summary.total_files == 2
    assert report.summary.complete  # 完整列出所选事实，不等于效果全部已知或执行成功。
    assert report.summary.stop_reason == "failed"
    check(report)


@pytest.mark.parametrize("reason", ["completed", "cancelled", "timeout", "failed", "interrupted"])
def test_all_applied_never_hides_stop_reason(group, reason):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = batch_diff_document(
        workspace,
        batch,
        ReadOperation(),
        output=output(batch, ("applied", "applied"), reason=reason),
    )
    assert report.summary.effect == "applied" and report.summary.stop_reason == reason
    check(report)


def test_not_started_has_all_pending_rows_and_no_effect_edits(group):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = batch_diff_document(
        workspace,
        batch,
        ReadOperation(),
        output=output(batch, ("pending", "pending"), phase="not_started", reason=None),
    )
    assert not report.edits and report.summary.complete and report.summary.effect == "not_applied"
    check(report)


@pytest.mark.parametrize("limit", [1024, 1600, 2000, 2400, 4000, 65536, 1048576])
def test_budget_preserves_every_member_then_an_edit_prefix(group, limit):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    full = batch_diff_document(workspace, batch, ReadOperation())
    try:
        report = batch_diff_document(
            workspace,
            batch,
            ReadOperation(),
            options=BatchDiffDocumentOptions(max_output_bytes=limit),
        )
    except KernelError as error:
        assert error.code == "patch_diff_budget_too_small"
        mandatory = full.model_copy(
            update={
                "edits": (),
                "summary": full.summary.model_copy(update={"returned_edits": 0, "complete": False}),
            }
        )
        assert len(mandatory.to_jsonl()) > limit
    else:
        assert report.files == full.files
        assert report.edits == full.edits[: len(report.edits)]
        assert len(check(report)) <= limit


def test_exact_budget_includes_every_newline_and_json_escape(group):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    full = batch_diff_document(workspace, batch, ReadOperation())
    size = len(full.to_jsonl())
    assert (
        batch_diff_document(
            workspace,
            batch,
            ReadOperation(),
            options=BatchDiffDocumentOptions(max_output_bytes=size),
        )
        == full
    )
    smaller = batch_diff_document(
        workspace,
        batch,
        ReadOperation(),
        options=BatchDiffDocumentOptions(max_output_bytes=size - 1),
    )
    assert len(smaller.edits) == len(full.edits) - 1 and not smaller.summary.complete
    check(smaller)


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4, 1024, 4096])
def test_utf8_preview_budget_and_full_hashes(tmp_path, limit):
    old, new = '旧🙂\t"\\\r\n' * 600, "新值\r\n" * 600
    (tmp_path / "main.py").write_bytes(old.encode())
    with Workspace(tmp_path) as workspace:
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(files=(proposal(workspace, old=old, new=new),)),
            ReadOperation(),
        )
        report = batch_diff_document(
            workspace, batch, ReadOperation(), options=BatchDiffDocumentOptions(preview_bytes=limit)
        )
        previous = patch_batch_diff(
            workspace, batch, ReadOperation(), PatchDiffOptions(preview_bytes=limit)
        )
        assert report.edits[0].before == previous.edits[0].before
        assert report.edits[0].after == previous.edits[0].after
        assert not report.summary.complete
        check(report)


def test_maximum_escaped_record_and_512_edits_stay_page_bounded(tmp_path):
    edits = tuple(
        ExactEdit(old_text=f"OLD-{i:02d}" + '\t"\\' * 100, new_text=f"NEW-{i:02d}" + '\t"\\' * 100)
        for i in range(32)
    )
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
        full = batch_diff_document(
            workspace,
            batch,
            ReadOperation(),
            options=BatchDiffDocumentOptions(max_output_bytes=1048576, preview_bytes=4096),
        )
        assert len(full.edits) == 512 and full.summary.complete
        check(full)
        limited = batch_diff_document(workspace, batch, ReadOperation())
        assert len(limited.edits) < 512 and not limited.summary.complete
        assert limited.files == full.files
        assert limited.edits == full.edits[: len(limited.edits)]
        check(limited)


@pytest.mark.parametrize("change", ["order", "sha", "unfinished", "corrupt_image"])
def test_rejects_output_or_image_mismatch(group, change):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    result = output(batch, ("applied", "applied"))
    if change == "order":
        result = result.model_copy(update={"files": result.files[::-1]})
    elif change == "sha":
        result = result.model_copy(
            update={
                "files": (
                    result.files[0].model_copy(update={"before_sha256": "0" * 64}),
                    result.files[1],
                )
            }
        )
    elif change == "unfinished":
        result = result.model_copy(update={"phase": "started", "stop_reason": None})
    else:
        batch = replace(batch, patches=(replace(batch.patches[0], before=b"bad"), batch.patches[1]))
    with pytest.raises(KernelError):
        batch_diff_document(workspace, batch, ReadOperation(), output=result)


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_cancel_and_timeout_before_render(group, mode):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    op = ReadOperation()
    if mode == "cancel":
        op.stopped.set()
    else:
        op.deadline = 0
    with pytest.raises(TurnCancelled if mode == "cancel" else KernelError):
        batch_diff_document(workspace, batch, op)


@pytest.mark.parametrize(
    "patch",
    [
        {"max_output_bytes": True},
        {"max_output_bytes": 1023},
        {"max_output_bytes": 1048577},
        {"preview_bytes": True},
        {"preview_bytes": -1},
        {"preview_bytes": 4097},
        {"approved": True},
    ],
)
def test_options_are_strict(patch):
    with pytest.raises(ValidationError):
        BatchDiffDocumentOptions.model_validate(patch)


@pytest.mark.parametrize(
    "change", ["complete", "count", "index", "order", "fingerprint", "range", "view", "hidden"]
)
def test_contract_rejects_forged_counts_selection_and_coordinates(group, change):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    report = batch_diff_document(workspace, batch, ReadOperation())
    data = json.loads(report.model_dump_json())
    if change == "complete":
        data["summary"]["complete"] = False
    elif change == "count":
        data["summary"]["eligible_files"] = 0
    elif change == "index":
        data["files"][0]["index"] = 1
    elif change == "order":
        data["edits"].reverse()
    elif change == "fingerprint":
        data["edits"][0]["patch_fingerprint"] = "0" * 64
    elif change == "range":
        data["edits"][0]["after_start"] += 1
    elif change == "view":
        data["summary"]["view"] = "effect"
    else:
        data["files"].pop()
    with pytest.raises(ValidationError):
        BatchDiffDocument.model_validate_json(json.dumps(data))


def test_largest_escaped_previews_fit_one_record(tmp_path):
    old, new = "\t" * 4095 + "a", "\\" * 4095 + "b"
    path = "/".join(["deep-" + "z" * 95] * 8 + ["main.py"])
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(old.encode())
    with Workspace(tmp_path) as workspace:
        batch = prepare_patch_batch(
            workspace,
            PatchBatchProposal(files=(proposal(workspace, path=path, old=old, new=new),)),
            ReadOperation(),
        )
        report = batch_diff_document(
            workspace, batch, ReadOperation(), options=BatchDiffDocumentOptions(preview_bytes=4096)
        )
        assert report.summary.complete and len(report.edits) == 1
        body = check(report)
        assert len(body.splitlines()[-1]) > 16000
        assert max(len(line) + 1 for line in body.splitlines()) <= MAX_RECORD_BYTES
