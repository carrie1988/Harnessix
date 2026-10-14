"""真实搜索快照SHA接入产品Patch审批/发布；ScriptedProvider不证明模型自主能力。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent, TurnStatus
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.tools.runtime import CodingToolRuntime
from tests.delivery.test_trusted_action_patch import _approval, _proposal
from tests.product_config.test_patch_precondition_rejection import CODE, MESSAGE, patch_turn
from tests.product_config.test_product_patch_rollback import approve, product, result
from tests.tools.test_files import execute

PREIMAGES = (
    pytest.param(b"old\n", id="lf"),
    pytest.param(b"old\r\n", id="crlf"),
    pytest.param(b"\xef\xbb\xbfold\n", id="bom"),
    pytest.param("old = 'é e\u0301 中文🙂'\n".encode(), id="unicode"),
    pytest.param(
        "\ufeffheader\r\nold = 'é e\u0301 中文🙂'\r\n尾行\r\n".encode(),
        id="crlf-bom-unicode",
    ),
    pytest.param(b"before\n" * 4 + b"old\n" + b"after\n" * 4, id="outside-context"),
)


async def grep_snapshot(root, before):
    # 只读Runtime与产品Patch共享规范根，不注入摘要，也不替换文件执行器。
    async with CodingToolRuntime(root, enable_grep_snapshots=True) as tools:
        assert tools.workspace_root == root.resolve()
        searched = await execute(tools, "grep", path="src/modified.py", query="old")
    assert searched.outcome == "succeeded"
    output = searched.output
    assert output["spec_version"] == "harnessix.grep-snapshot/v1"
    assert output["scan_complete"] and not output["truncated"]
    assert output["stats"]["files_read"] == 1
    assert output["stats"]["bytes_read"] == len(before)
    assert len(output["matches"]) == 1
    hit = output["matches"][0]
    assert hit["path"] == "src/modified.py"
    assert hit["content_sha256"] == hashlib.sha256(before).hexdigest()
    assert hit["file_bytes"] == len(before)
    assert hit["revision"] != hit["content_sha256"]
    assert not hit["text_truncated"]
    assert hit["content_sha256"] != hashlib.sha256(hit["text"].encode()).hexdigest()
    context = "\n".join(line["text"] for line in hit["context"]["lines"]).encode()
    assert hit["content_sha256"] != hashlib.sha256(context).hexdigest()
    return hit


def snapshot_proposal(hit, before):
    original = _proposal()
    assert original.files[0].path == hit["path"]
    # 保留目标文件的换行/BOM/Unicode与长度，避免混入全文过量删减负控。
    # 新增和删除项仍使用原fixture；deleted.txt的LF前镜像及摘要保持不变。
    modified = original.files[0].model_copy(
        update={
            "expected_sha256": hit["content_sha256"],
            "content": before.replace(b"old", b"new").decode("utf-8"),
        }
    )
    return original.model_copy(update={"files": (modified, *original.files[1:])})


def workspace_image(root):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("before", PREIMAGES)
async def test_grep_complete_content_sha_publishes_real_product_patch(tmp_path, before):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        target = root / "src/modified.py"
        target.write_bytes(before)
        hit = await grep_snapshot(root, before)
        proposal = snapshot_proposal(hit, before)
        unchanged = workspace_image(root)

        thread, waiting, call = await patch_turn(runtime, provider, root, proposal)

        assert call.arguments["files"][0]["expected_sha256"] == hit["content_sha256"]
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        request = _approval(waiting)
        assert request.plan_id == trusted_action_invocation_id(
            thread.thread_id, waiting.turn_id, call
        )
        assert request.presentation == "patch_batch" and request.diff_artifact is not None
        assert router.status(request.plan_id).state == "pending_approval"
        assert router.approval(request.plan_id) is None
        assert workspace_image(root) == unchanged
        prepared = transactions.load(request.plan_id)
        assert prepared.state == "prepared"
        mutation = next(item for item in prepared.plan.mutations if item.path == hit["path"])
        assert mutation.before.sha256 == hit["content_sha256"]
        assert mutation.before.size == hit["file_bytes"]
        assert transactions.blob(mutation.before.sha256) == before

        completed = await approve(runtime, thread.thread_id, waiting)

        output = result(completed)
        assert completed.status is TurnStatus.COMPLETED
        assert output.outcome == "succeeded" and output.error is None
        assert output.output["transaction_id"] == str(request.plan_id)
        assert output.output["files"] == 3
        assert router.status(request.plan_id).state == "succeeded"
        assert router.approval(request.plan_id).decision.outcome is ApprovalOutcome.APPROVED
        published = transactions.load(request.plan_id)
        assert published.state == "published" and published.cursor == 3
        assert published.plan == prepared.plan
        assert target.read_bytes() == before.replace(b"old", b"new")
        assert (root / "src/新增.py").read_bytes() == "print('新增')\n".encode()
        assert not (root / "tests/deleted.txt").exists()
        assert set(workspace_image(root)) == {"src/modified.py", "src/新增.py"}


@pytest.mark.parametrize("before", PREIMAGES)
@pytest.mark.parametrize("invalid", ["source-drift", "revision-as-sha"])
async def test_stale_or_revision_snapshot_is_rejected_without_approval_or_effect(
    tmp_path, before, invalid
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        target = root / "src/modified.py"
        target.write_bytes(before)
        hit = await grep_snapshot(root, before)
        proposal = snapshot_proposal(hit, before)
        if invalid == "source-drift":
            # 命中行保持不变；命中以外的字节漂移仍使完整文件SHA失效。
            target.write_bytes(before + b"# source drift\n")
        else:
            modified = proposal.files[0].model_copy(update={"expected_sha256": hit["revision"]})
            proposal = proposal.model_copy(update={"files": (modified, *proposal.files[1:])})
        unchanged = workspace_image(root)

        thread, denied, call = await patch_turn(runtime, provider, root, proposal)

        output = result(denied)
        assert denied.status is TurnStatus.COMPLETED
        assert output.outcome == "failed" and output.trusted_action is None
        assert output.error is not None
        assert output.error.code == CODE and output.error.message == MESSAGE
        assert not output.error.retryable
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in denied.items
        )
        plan_id = trusted_action_invocation_id(thread.thread_id, denied.turn_id, call)
        assert router.status(plan_id).state == "denied"
        # 系统拒绝审计不是用户审批请求；拒绝后的原Route也不可再次批准。
        decision = router.approval(plan_id).decision
        assert (decision.outcome, decision.actor, decision.reason) == (
            ApprovalOutcome.REJECTED,
            "system.validation",
            CODE,
        )
        assert all(event.executor_id is None for event in router.events(plan_id))
        with pytest.raises(KernelError) as missing:
            transactions.load(plan_id)
        assert missing.value.code == "delivery_transaction_not_found"
        for table in ("workspace_transactions", "workspace_transaction_events"):
            assert transactions._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert not list((tmp_path / "state/delivery/blobs").iterdir())
        with pytest.raises(KernelError) as conflict:
            router.decide(plan_id, ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user"))
        assert conflict.value.code == "approval_conflict"
        assert router.status(plan_id).state == "denied"
        assert workspace_image(root) == unchanged
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert not (root / "src/新增.py").exists()
