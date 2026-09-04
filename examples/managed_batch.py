"""真实多文件一次性执行、读回与重开只核对；不调用模型。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


def exercise(root: Path) -> None:
    source = root / "source"
    source.mkdir()
    paths = ("main.py", "test_main.py")
    for name in paths:
        (source / name).write_bytes(b"before\r\n")
    factory = PatchWorkspaces(root / "private")
    with Workspace(source) as workspace, factory.create(workspace, paths, ReadOperation()) as copy:
        proposal = PatchBatchProposal(
            files=tuple(
                PatchProposal(
                    path=name,
                    expected_revision=read_file(
                        copy.workspace, ReadFileInput(path=name), ReadOperation()
                    ).revision,
                    edits=(ExactEdit(old_text="before", new_text="after"),),
                )
                for name in paths
            )
        )
        batch = prepare_patch_batch(copy.workspace, proposal, ReadOperation())
        groups = ManagedPatchBatches(copy)
        pending = groups.save(batch, "host-request-1", ReadOperation())
        assert pending.decision is None
        approved = groups.reply(
            pending.plan.batch_id,
            pending.plan.approval_fingerprint,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="本地宿主"),
            ReadOperation(),
        )
        result = groups.execute(
            approved.plan.batch_id, approved.plan.approval_fingerprint, ReadOperation()
        )
        assert result.effect == "applied" and result.run.stop_reason == "completed"
        workspace_id = copy.workspace_id
    with factory.open(workspace_id) as copy:
        groups = ManagedPatchBatches(copy)
        assert groups.lookup("host-request-1", ReadOperation()) == approved
        assert groups.get_execution(approved.plan.batch_id, ReadOperation()) == result
        assert groups.reconcile(approved.plan.batch_id, ReadOperation()) == result
        for member in approved.plan.members:
            assert copy.get(member.plan_id).state == "applied"
            try:
                copy.execute(member.plan_id, member.approval_fingerprint, ReadOperation())
            except KernelError as error:
                assert error.code == "patch_batch_member_requires_group"
            else:
                raise AssertionError("旧入口不应消费组成员")
        for name in paths:
            assert (copy.workspace.root / name).read_bytes() == b"after\r\n"
            assert (source / name).read_bytes() == b"before\r\n"
    print("多文件真实写入、读回、重开只核对及禁止拆分消费通过；源目录未修改。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-batch-run-") as directory:
        exercise(Path(directory))


if __name__ == "__main__":
    main()
