"""实际子进程 os._exit 切断，不用异常 mock 代替崩溃恢复证据。"""

import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.planner import prepare_patch
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace

EXECUTE = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.patches import managed
from harnessix.tools.workspace import ReadOperation
root, workspace_id, plan_id, fingerprint, cut = sys.argv[1:]
managed._fault = lambda point: os._exit(73) if point == cut else None
with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
    copy.execute(UUID(plan_id), fingerprint, ReadOperation())
raise AssertionError('没有到达退出切点')
"""
BUILD = """
import os, sys
from pathlib import Path
from harnessix.patches import managed
from harnessix.tools.workspace import ReadOperation, Workspace
root, source, cut = sys.argv[1:]
managed._fault = lambda point: os._exit(74) if point == cut else None
with Workspace(Path(source)) as workspace:
    managed.PatchWorkspaces(Path(root)).create(workspace, ['main.py'], ReadOperation())
raise AssertionError('没有到达退出切点')
"""


@pytest.mark.parametrize("path", ["main.py", "nested/main.py"])
@pytest.mark.parametrize(
    "cut,expected",
    [
        ("started", "observed_before"),
        ("temp_created", "observed_before"),
        ("temp_synced", "observed_before"),
        ("temp_recorded", "observed_before"),
        ("before_replace", "observed_before"),
        ("after_replace", "observed_after"),
        ("directories_synced", "observed_after"),
        ("before_result", "observed_after"),
        ("result_recorded", "applied"),
    ],
)
def test_real_process_exit_no_write_replay(tmp_path, path, cut, expected):
    source_path = tmp_path / "source"
    target = source_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before\r\n")
    factory = PatchWorkspaces(tmp_path / "private")
    with Workspace(source_path) as source, factory.create(source, [path], ReadOperation()) as copy:
        revision = read_file(copy.workspace, ReadFileInput(path=path), ReadOperation()).revision
        prepared = prepare_patch(
            copy.workspace,
            PatchProposal(
                path=path,
                expected_revision=revision,
                edits=(ExactEdit(old_text="before", new_text="after"),),
            ),
            ReadOperation(),
        )
        record = copy.save(prepared, "crash", ReadOperation())
        copy.reply(
            record.plan_id,
            record.approval_fingerprint,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="故障验收"),
        )
        workspace_id = copy.workspace_id
        copy_path = copy.workspace.root / path
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            EXECUTE,
            str(factory.root),
            str(workspace_id),
            str(record.plan_id),
            record.approval_fingerprint,
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 73, result.stderr
    before_recovery = copy_path.stat()
    expected_body = b"before\r\n" if expected == "observed_before" else b"after\r\n"
    assert copy_path.read_bytes() == expected_body
    with factory.open(workspace_id) as recovered:
        observed = recovered.reconcile(record.plan_id, ReadOperation())
        assert observed.state == expected
        assert recovered.reconcile(record.plan_id, ReadOperation()) == observed
        with pytest.raises(KernelError) as error:
            recovered.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
        assert error.value.code == "patch_not_executable"
    after_recovery = copy_path.stat()
    assert before_recovery.st_ino == after_recovery.st_ino
    assert before_recovery.st_mtime_ns == after_recovery.st_mtime_ns
    assert before_recovery.st_ctime_ns == after_recovery.st_ctime_ns
    assert copy_path.read_bytes() == expected_body
    assert target.read_bytes() == b"before\r\n"


@pytest.mark.parametrize("cut", ["copy_building", "copy_before_ready"])
def test_real_exit_building_copy_is_quarantined(tmp_path, cut):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("before")
    factory = PatchWorkspaces(tmp_path / "private")
    result = subprocess.run(
        [sys.executable, "-c", BUILD, str(factory.root), str(source), cut],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 74, result.stderr
    (bundle,) = factory.root.iterdir()
    with pytest.raises(KernelError) as error:
        factory.open(UUID(bundle.name))
    assert error.value.code == "patch_copy_not_ready"
    assert (source / "main.py").read_text() == "before"
    assert Path(bundle).exists()
