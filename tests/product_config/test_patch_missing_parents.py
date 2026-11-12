"""产品路径：apply_patch_batch在不存在的目录里新建文件（ADR 0137）。

真实Gateway、Agent、SQLite与工作区；两种装配各跑一遍：父目录作为显式资源（第1代快照），
以及正式产品使用的父目录历史（第2代快照，计划与审计账本经同一CAS读取）。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import workspace_patch_binding
from harnessix.delivery.trusted_action_contracts import parse_workspace_action_review
from harnessix.domain.models import ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_ui.diff_view import readable_diff
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.preparation_rejection import workspace_patch_planning_rejection
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.blob_read_control import WorkspaceBlobReader
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _approval
from tests.product_config.test_patch_call_repair import raw_patch_step
from tests.product_config.test_product_patch_rollback import approve, result, rollback_step

pytestmark = pytest.mark.skipif(os.name != "posix", reason="只有POSIX事务端口补建父目录")
GENERATIONS = pytest.mark.parametrize("closure", [False, True], ids=["snapshot-v1", "snapshot-v2"])

OLD = "def value():\n    return 1\n"
EDIT = {
    "operation": "edit",
    "path": "src/app.py",
    "expected_sha256": hashlib.sha256(OLD.encode()).hexdigest(),
    "edits": [{"old_text": "return 1", "new_text": "return 2"}],
}


def create(path: str, content: str = "x\n") -> dict[str, object]:
    return {"operation": "create", "path": path, "content": content, "mode": 420}


# Beta回归（state-ocr事件569、583、765）：tests/不存在时新建tests/下的文件（含空的__init__.py），
# 同批还有已有目录里的新文件和对已有文件的编辑；原来整批以workspace_parent_missing失败。
BETA_BATCH = [
    create("src/upload.py", "LIMIT = 1\n"),
    EDIT,
    create("tests/__init__.py", ""),
    create("tests/test_x.py", "def test_x():\n    assert True\n"),
]


@asynccontextmanager
async def product(tmp_path: Path, *, closure: bool):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_bytes(OLD.encode())
    environment = build_fixed_product_action_environment(root)
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "state/delivery")
    reader = (
        WorkspaceBlobReader(transactions.blob, transactions.controlled_blob) if closure else None
    )
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db", read_blob=reader)
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db", read_blob=reader)
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    sessions = SQLiteSessionStore(tmp_path / "state/sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
        snapshot_ports=(
            WorkspaceSnapshotPorts(
                transactions.put_blob, transactions.blob, transactions.controlled_blob
            )
            if closure
            else None
        ),
    )
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        composition = build_product_action_composition(
            build_product_action_config(),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
        )
        provider = ScriptedProvider([])
        try:
            async with AgentRuntime(
                sessions,
                provider,
                trusted_actions=composition.gateway,
                artifacts=artifacts,
                scoped_tools=tools,
            ) as runtime:
                yield root, runtime, provider, artifacts
        finally:
            if composition.gateway is not None:
                composition.gateway.close()
            plans.close()
            audit.close()
            transactions.close()
            leases.close()


async def run_patch(runtime, provider, root, files):
    provider.steps = (raw_patch_step({"files": files}), answer("已处理"))
    thread = await runtime.create_thread(str(root))
    turn = await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")
    return thread, turn


async def publish(runtime, provider, root, files):
    thread, waiting = await run_patch(runtime, provider, root, files)
    assert waiting.status is TurnStatus.WAITING_APPROVAL, result(waiting).error
    completed = await approve(runtime, thread.thread_id, waiting)
    assert result(completed).outcome == "succeeded"
    return thread, _approval(waiting)


async def rollback(runtime, provider, thread, transaction_id):
    provider.steps = (rollback_step(transaction_id), answer("回滚完成"))
    waiting = await runtime.run_turn(thread.thread_id, "撤销上次修改", request_id="rollback")
    assert waiting.status is TurnStatus.WAITING_APPROVAL
    completed = await approve(runtime, thread.thread_id, waiting)
    return next(
        item.content
        for item in reversed(completed.items)
        if getattr(item.content, "kind", None) == "tool_result"
    )


def tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def has_approval(turn) -> bool:
    return any(isinstance(i.content, TrustedActionApprovalRequestContent) for i in turn.items)


@GENERATIONS
async def test_batch_creating_files_in_a_new_directory_is_reviewed_and_applied(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, artifacts):
        thread, waiting = await run_patch(runtime, provider, root, BETA_BATCH)

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        # 批准之前工作区没有任何变化，目录也还没有建。
        assert tree(root) == {"src": None, "src/app.py": OLD.encode()}
        request = _approval(waiting)
        page = await artifacts.read(
            thread.thread_id, runtime.tools.workspace_scope, request.diff_artifact.artifact_id
        )
        review = parse_workspace_action_review(page.text.encode())
        diff = "".join(chunk.text for chunk in review.chunks)
        # 审批给用户看的是完整效果：四个文件，加上将要新建的目录。
        assert diff.startswith("new directory tests\ndiff --harnessix a/src/app.py b/src/app.py\n")
        assert [(item.entry.kind, item.entry.path) for item in review.entries] == [
            ("modified", "src/app.py"),
            ("added", "src/upload.py"),
            ("added", "tests/__init__.py"),
            ("added", "tests/test_x.py"),
        ]
        shown = [line.plain for line in readable_diff(page.text)]
        assert shown[:8] == [
            "共 4 个文件：",
            "  修改  src/app.py",
            "  新增  src/upload.py",
            "  新增  tests/__init__.py",
            "  新增  tests/test_x.py",
            "  新建目录  tests",
            "",
            "── src/app.py ──",
        ]

        completed = await approve(runtime, thread.thread_id, waiting)

        output = result(completed)
        assert output.outcome == "succeeded" and output.output["files"] == 4
        assert output.output["diff_preview"]["text"].startswith("new directory tests\n")
        assert tree(root) == {
            "src": None,
            "src/app.py": OLD.replace("return 1", "return 2").encode(),
            "src/upload.py": b"LIMIT = 1\n",
            "tests": None,
            "tests/__init__.py": b"",
            "tests/test_x.py": b"def test_x():\n    assert True\n",
        }


@GENERATIONS
async def test_batch_rejected_for_another_entry_creates_no_directory(tmp_path, closure):
    """整批原子：同批里一处编辑的锚点不存在，新目录和其中的文件都不出现。"""
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        before = tree(root)
        stale = {**EDIT, "edits": [{"old_text": "return 7", "new_text": "return 2"}]}
        _, turn = await run_patch(runtime, provider, root, [create("tests/__init__.py", ""), stale])

        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED and output.outcome == "failed"
        assert output.error.code == "patch_context_not_found"
        assert not has_approval(turn) and tree(root) == before


@GENERATIONS
async def test_batch_the_user_rejects_creates_no_directory(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        before = tree(root)
        thread, waiting = await run_patch(runtime, provider, root, BETA_BATCH)

        completed = await approve(
            runtime, thread.thread_id, waiting, outcome=ApprovalOutcome.REJECTED
        )

        assert result(completed).outcome != "succeeded" and tree(root) == before


@GENERATIONS
async def test_rollback_removes_the_files_and_the_directories_the_patch_created(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        (root / "empty").mkdir()
        before = tree(root)
        files = [*BETA_BATCH, create("docs/guide/intro.md"), create("empty/kept.py")]
        thread, request = await publish(runtime, provider, root, files)
        assert (root / "docs/guide").is_dir() and (root / "tests").is_dir()

        output = await rollback(runtime, provider, thread, request.plan_id)

        assert output.outcome == "succeeded"
        # 回到补丁之前：新建的目录一并删除；原本就有的目录（即使回滚后是空的）保留。
        assert tree(root) == before


@GENERATIONS
async def test_rollback_keeps_a_created_directory_that_now_holds_other_entries(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        files = [create("docs/guide/intro.md"), create("tests/test_x.py")]
        thread, request = await publish(runtime, provider, root, files)
        (root / "docs/notes.txt").write_bytes(b"written by the user later\n")

        output = await rollback(runtime, provider, thread, request.plan_id)

        assert output.outcome == "succeeded"
        assert tree(root) == {
            "docs": None,
            "docs/notes.txt": b"written by the user later\n",
            "src": None,
            "src/app.py": OLD.encode(),
        }


@GENERATIONS
async def test_rollback_of_a_delete_recreates_a_directory_removed_since(tmp_path, closure):
    """回滚一次删除要重新建出文件；它的目录在此期间被删掉时，由逆向事务补建。"""
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        before = tree(root)
        delete = {
            "operation": "delete",
            "path": "src/app.py",
            "expected_sha256": hashlib.sha256(OLD.encode()).hexdigest(),
        }
        thread, request = await publish(runtime, provider, root, [delete])
        (root / "src").rmdir()

        output = await rollback(runtime, provider, thread, request.plan_id)

        assert output.outcome == "succeeded"
        assert tree(root) == before


@GENERATIONS
@pytest.mark.parametrize(
    ("path", "code", "says"),
    [
        ("plain.txt/new.py", "workspace_wrong_file_type", "is not a directory"),
        ("plain.txt/deeper/new.py", "workspace_wrong_file_type", "is not a directory"),
        ("link/new.py", "workspace_path_denied", "symbolic link"),
        ("link/deeper/new.py", "workspace_path_denied", "symbolic link"),
    ],
)
async def test_existing_parent_that_is_not_a_directory_is_a_correctable_failure(
    tmp_path, closure, path, code, says
):
    """父路径上已有普通文件或符号链接：未执行的失败结果，说明原因和下一步，Turn不被中断。"""
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "plain.txt").write_bytes(b"plain\n")
        (root / "link").symlink_to(outside)
        before = tree(root)

        _, turn = await run_patch(runtime, provider, root, [create(path)])

        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED and output.outcome == "failed"
        assert output.error.code == code
        assert says in output.error.message and output.error.message.isascii()
        assert not has_approval(turn)
        assert tree(root) == before and list(outside.iterdir()) == []


@GENERATIONS
async def test_one_path_used_as_file_and_as_new_directory_is_rejected_whole(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        before = tree(root)

        _, turn = await run_patch(runtime, provider, root, [create("pkg"), create("pkg/a.py")])

        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED and output.outcome == "failed"
        assert output.error.code == "delivery_path_case_conflict"
        assert output.error.message.endswith("本次Patch未执行，工作区没有变化")
        assert tree(root) == before


def test_platform_that_cannot_create_parents_says_what_to_do_next():
    """Windows事务端口仍不补建父目录：同一错误码，写给模型的是英文并给出可行的下一步。"""
    # 该投影只用到调用身份与文件项数；其余调用字段与本断言无关。
    call = SimpleNamespace(call_id=uuid4(), arguments={"files": [{"path": "tests/__init__.py"}]})
    rejected = workspace_patch_planning_rejection(
        workspace_patch_binding(),
        call,
        KernelError("workspace_parent_missing", "Windows缺失资源的父目录不存在"),
    )

    assert rejected is not None and rejected.outcome == "failed"
    assert rejected.error.code == "workspace_parent_missing"
    assert rejected.error.message.isascii()
    assert "does not create directories" in rejected.error.message
    assert "command tool" in rejected.error.message and "mkdir" in rejected.error.message
    # 与其余被拒批次一致：写明这一批什么都没有写入（ADR 0129第2版）。
    assert rejected.error.message.endswith("Nothing was written.")
