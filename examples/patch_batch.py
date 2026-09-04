"""真实多文件只读计划与有界结构化 Diff；不执行或批准整组写入。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch, verify_patch_batch
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.diff import patch_batch_diff
from harnessix.patches.diff_contracts import PatchBatchDiff, PatchDiffOptions
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


def exercise(root: Path) -> None:
    originals = {
        "main.py": '\ufeffprint("before")\r\n',
        "test_main.py": 'assert result == "before"\n',
    }
    for name, text in originals.items():
        (root / name).write_bytes(text.encode())
    with Workspace(root) as workspace:
        proposals = tuple(
            PatchProposal(
                path=name,
                expected_revision=read_file(
                    workspace, ReadFileInput(path=name), ReadOperation()
                ).revision,
                edits=(ExactEdit(old_text="before", new_text="after"),),
            )
            for name in originals
        )
        batch = prepare_patch_batch(workspace, PatchBatchProposal(files=proposals), ReadOperation())
        report = patch_batch_diff(workspace, batch, ReadOperation())
        assert report.total_files == 2 and report.total_edits == 2 and not report.truncated
        assert PatchBatchDiff.model_validate_json(report.model_dump_json()) == report
        for patch, edit in zip(batch.patches, report.edits, strict=True):
            start, old, new = edit.before_start, edit.before.text.encode(), edit.after.text.encode()
            assert patch.before[start : start + len(old)] == old
            assert patch.before[:start] + new + patch.before[start + len(old) :] == patch.after
        short = patch_batch_diff(
            workspace, batch, ReadOperation(), PatchDiffOptions(max_output_bytes=256)
        )
        assert short.truncated and not short.edits and len(short.model_dump_json().encode()) <= 256
    with Workspace(root) as reopened:
        verify_patch_batch(reopened, batch, ReadOperation())
    assert sorted(p.name for p in root.iterdir()) == sorted(originals)
    for name, text in originals.items():
        assert (root / name).read_bytes() == text.encode()
    print(
        "多文件计划、字节坐标重建、有界 Diff、重开复核通过；"
        "没有批准、写入、模型 API 或 Artifact 发布。"
    )


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-batch-") as directory:
        exercise(Path(directory))


if __name__ == "__main__":
    main()
