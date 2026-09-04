"""用真实旧 v2/new v3 wheel 验收组审批升级；不依赖源码测试包或模型。"""

import json
import sys
from pathlib import Path
from uuid import UUID

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

PATHS = ("one.py", "two.py", "three.py")


def files(root):
    return [
        [p.read_bytes().hex(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns]
        for p in (root / path for path in PATHS)
    ]


def archive(copy):
    return json.loads(
        json.dumps(
            {
                "tables": [
                    copy._db.execute("SELECT * FROM metadata").fetchall(),
                    copy._db.execute(
                        "SELECT path,hex(body) FROM baseline ORDER BY path"
                    ).fetchall(),
                    copy._db.execute(
                        "SELECT id,request_id,proposal,hex(before_image),hex(after_image),"
                        "owner_batch_id "
                        "FROM plans ORDER BY id"
                    ).fetchall(),
                    copy._db.execute("SELECT * FROM events ORDER BY sequence").fetchall(),
                    copy._db.execute("SELECT * FROM batches ORDER BY id").fetchall(),
                    copy._db.execute("SELECT * FROM batch_approvals ORDER BY batch_id").fetchall(),
                ],
                "files": files(copy.workspace.root),
                "database_inode": (copy._bundle.root / "ledger.sqlite").stat().st_ino,
            }
        )
    )


def create(root):
    source = root / "source"
    source.mkdir(parents=True)
    for name in PATHS:
        (source / name).write_bytes(b"before\r\n")
    factory = PatchWorkspaces(root / "private")
    with Workspace(source) as workspace, factory.create(workspace, PATHS, ReadOperation()) as copy:
        assert copy._db.execute("PRAGMA user_version").fetchone()[0] == 2
        batch = prepare_patch_batch(
            copy.workspace,
            PatchBatchProposal(
                files=tuple(
                    PatchProposal(
                        path=name,
                        expected_revision=read_file(
                            copy.workspace, ReadFileInput(path=name), ReadOperation()
                        ).revision,
                        edits=(ExactEdit(old_text="before", new_text="after"),),
                    )
                    for name in PATHS
                )
            ),
            ReadOperation(),
        )
        groups = ManagedPatchBatches(copy)
        saved = []
        for name in ("pending", "approved", "rejected"):
            approval = groups.save(batch, name, ReadOperation())
            if name != "pending":
                approval = groups.reply(
                    approval.plan.batch_id,
                    approval.plan.approval_fingerprint,
                    ApprovalDecision(outcome=ApprovalOutcome(name), actor="升级验收"),
                    ReadOperation(),
                )
            saved.append(approval.model_dump(mode="json"))
        data = {
            "workspace_id": str(copy.workspace_id),
            "archive": archive(copy),
            "approvals": saved,
            "source": files(source),
        }
    (root / "audit.json").write_text(json.dumps(data))
    print("旧 v2 wheel 已创建等待、批准、拒绝三类真实组记录，未执行。")


def verify(root, mode):
    data = json.loads((root / "audit.json").read_text())
    factory = PatchWorkspaces(root / "private")
    try:
        copy = factory.open(UUID(data["workspace_id"]))
    except KernelError as error:
        assert mode == "reject" and error.code == "patch_wrong_database"
        print("旧 v2 reader 已明确拒绝 v3，没有改写或执行。")
        return
    with copy:
        assert mode != "reject"
        assert copy._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert archive(copy) == data["archive"]
        groups = ManagedPatchBatches(copy)
        for expected in data["approvals"]:
            approval = groups.lookup(expected["plan"]["request_id"], ReadOperation())
            assert approval.model_dump(mode="json") == expected
            assert groups.get_execution(approval.plan.batch_id, ReadOperation()) is None
        if mode == "execute":
            plan = groups.lookup("approved", ReadOperation()).plan
            result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
            assert result.run.stop_reason == "completed" and result.effect == "applied"
            after = files(copy.workspace.root)
            assert groups.reconcile(plan.batch_id, ReadOperation()) == result
            assert files(copy.workspace.root) == after
        assert files(root / "source") == data["source"]
    print("新 v3 wheel 保留所有旧记录原字节；升级不执行，显式执行与只核对按指定模式验收。")


if __name__ == "__main__":
    mode, directory = sys.argv[1:]
    if mode == "create":
        create(Path(directory))
    elif mode in {"upgrade", "reject", "execute"}:
        verify(Path(directory), mode)
    else:
        raise ValueError("模式必须为 create、upgrade、reject 或 execute")
