"""分别由真实旧/新 wheel 独立运行，验证副本升级、原字节保留和旧 reader 拒绝。"""

import json
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches import ledger
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.planner import prepare_patch
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace

PATHS = ("pending.py", "approved.py", "applied.py")


def files(root: Path) -> list[list[object]]:
    result: list[list[object]] = []
    for name in PATHS:
        target = root / name
        info = target.stat()
        result.append([target.read_bytes().hex(), info.st_ino, info.st_mtime_ns, info.st_ctime_ns])
    return result


def archive(copy):
    return {
        "ledger": [
            copy._db.execute("SELECT * FROM metadata").fetchall(),
            copy._db.execute("SELECT path,hex(body) FROM baseline ORDER BY path").fetchall(),
            copy._db.execute(
                "SELECT id,request_id,proposal,hex(before_image),hex(after_image) "
                "FROM plans ORDER BY id"
            ).fetchall(),
            copy._db.execute("SELECT * FROM events ORDER BY sequence").fetchall(),
        ],
        "files": files(copy.workspace.root),
        "database_inode": (copy._bundle.root / "ledger.sqlite").stat().st_ino,
    }


def create(root: Path) -> None:
    source = root / "source"
    source.mkdir(parents=True)
    for name in PATHS:
        (source / name).write_bytes(b"before\r\n")
    factory = PatchWorkspaces(root / "private")
    with Workspace(source) as workspace, factory.create(workspace, PATHS, ReadOperation()) as copy:
        assert copy._db.execute("PRAGMA user_version").fetchone()[0] == 1
        plans = []
        for index, name in enumerate(PATHS):
            revision = read_file(copy.workspace, ReadFileInput(path=name), ReadOperation()).revision
            patch = prepare_patch(
                copy.workspace,
                PatchProposal(
                    path=name,
                    expected_revision=revision,
                    edits=(ExactEdit(old_text="before", new_text="after"),),
                ),
                ReadOperation(),
            )
            record = copy.save(patch, name, ReadOperation())
            if index:
                record = copy.reply(
                    record.plan_id,
                    record.approval_fingerprint,
                    ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="升级验收"),
                )
            if index == 2:
                record = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
            plans.append(record.model_dump(mode="json"))
        payload = {
            "workspace_id": str(copy.workspace_id),
            "archive": archive(copy),
            "plans": plans,
            "source": files(source),
        }
    (root / "audit.json").write_text(json.dumps(payload))
    print("旧 wheel 已创建 v1 副本及 pending/approved/applied 三类真实计划。")


def verify(root: Path, *, old_reader: bool) -> None:
    payload = json.loads((root / "audit.json").read_text())
    factory = PatchWorkspaces(root / "private")
    try:
        copy = factory.open(UUID(payload["workspace_id"]))
    except KernelError as error:
        assert old_reader and error.code == "patch_wrong_database"
        print("旧 wheel 明确拒绝升级后的副本，没有降级或执行。")
        return
    with copy:
        assert not old_reader, "旧 reader 未拒绝新版本"
        assert copy._db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        assert json.loads(json.dumps(archive(copy))) == payload["archive"]
        assert files(root / "source") == payload["source"]
        for expected in payload["plans"]:
            assert copy.get(UUID(expected["plan_id"])).model_dump(mode="json") == expected
    print("新 wheel 升级通过：旧事件/镜像原字节、副本文件时间/inode、源目录全部保留。")


if __name__ == "__main__":
    mode, directory = sys.argv[1:]
    if mode == "create":
        create(Path(directory))
    elif mode in {"upgrade", "reject"}:
        verify(Path(directory), old_reader=mode == "reject")
    else:
        raise ValueError("模式必须为 create、upgrade 或 reject")
