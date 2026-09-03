"""受管副本上的真实单文件修改、审批和重开验收；不调用模型或运行仓库代码。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.planner import prepare_patch
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


def exercise(directory: Path) -> None:
    source = directory / "source"
    source.mkdir()
    original = b"def total(a, b):\n    return a - b\n"
    (source / "main.py").write_bytes(original)
    factory = PatchWorkspaces(directory / "private")
    with Workspace(source) as source_workspace:
        with factory.create(source_workspace, ["main.py"], ReadOperation()) as copy:
            page = read_file(copy.workspace, ReadFileInput(path="main.py"), ReadOperation())
            prepared = prepare_patch(
                copy.workspace,
                PatchProposal(
                    path="main.py",
                    expected_revision=page.revision,
                    edits=(ExactEdit(old_text="return a - b", new_text="return a + b"),),
                ),
                ReadOperation(),
            )
            record = copy.save(prepared, "fix-total-1", ReadOperation())
            assert record.state == "pending"
            assert (copy.workspace.root / "main.py").read_bytes() == original
            copy.reply(
                record.plan_id,
                record.approval_fingerprint,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="本地验收"),
            )
            assert (
                copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation()).state
                == "applied"
            )
            assert (copy.workspace.root / "main.py").read_bytes() == prepared.after
            assert (source / "main.py").read_bytes() == original
            workspace_id = copy.workspace_id
    with factory.open(workspace_id) as reopened:
        assert reopened.get(record.plan_id).state == "applied"
        assert reopened.reconcile(record.plan_id, ReadOperation()).state == "applied"
        try:
            reopened.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
        except KernelError as error:
            assert error.code == "patch_not_executable"
        else:
            raise AssertionError("已消费审批不得再次执行")
    print("受管副本真实修改、持久审批、重开不重写通过；源目录未变，未调用模型。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-managed-patch-") as directory:
        exercise(Path(directory))


if __name__ == "__main__":
    main()
