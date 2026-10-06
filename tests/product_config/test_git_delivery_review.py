"""Git 审批审阅：完整 CAS 数据合同与实际认证 SDK Producer 验收分离。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from time import monotonic

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import WorkspaceActionReviewChunk
from harnessix.domain.artifact_pagination import paginate_artifact_lines
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_delivery_review_codec import (
    build_product_git_action_review,
    decode_product_git_action_review,
    encode_product_git_action_review,
)
from harnessix.product_config.git_delivery_review_contracts import ProductGitActionReviewDocument
from harnessix.product_config.git_parent_contracts import ProductGitDeliverySourceV2
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.support.git_delivery_observed_core import canonical, json_facts
from tests.support.git_delivery_review import (
    material_case,
    pending_actual_review,
    review_document_like,
)
from tests.support.git_user_observation import run_authenticated_observation


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas") as store:
        yield GitMaterialCAS(store)


@pytest.fixture
def complete_review(cas, tmp_path):
    case, verified = material_case(cas, tmp_path)
    document = build_product_git_action_review(case.core, verified.diff, checkpoint=lambda: None)
    return case, verified, document


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_complete_cas_data_fixture_reads_core_and_full_diff_without_session_claim(
    cas, tmp_path, fmt
):
    """数据正控没有认证 Session；材料包含完整 Diff 与所有对象，而不是假事务计划。"""
    case, verified = material_case(cas, tmp_path, fmt=fmt)
    assert type(verified.core) is ProductGitDeliveryCoreV2
    assert verified.core == case.core
    assert verified.diff.content.text == case.diff_text
    assert verified.diff.content.sha256 == hashlib.sha256(case.diff_text.encode()).hexdigest()
    assert len(verified.diff.content.entries) == 1
    assert not hasattr(case, "session")


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("operation", ["checkpoint", "commit"])
def test_complete_cas_review_has_independent_canonical_jsonl_and_full_call(
    cas, tmp_path, fmt, operation
):
    """预期首记录与全文按字段独立定义；这里没有认证 Session 或执行批准。"""
    case, verified = material_case(cas, tmp_path, fmt=fmt, action=operation)
    core, content = case.core, verified.diff.content
    summary = {
        "record_type": "summary",
        "spec_version": "harnessix.product-git-action-review/v1",
        "core_fingerprint": core.fingerprint,
        "delivery_id": str(core.delivery_id),
        "call": json_facts(core.call),
        "workspace_revision": core.baseline.source.workspace.revision,
        "base_commit_oid": core.baseline.head_oid,
        "target_tree_oid": core.object_scope.roots.target_tree.object_id,
        "file_count": len(content.entries),
        "diff_utf8_bytes": len(case.diff_text.encode("utf-8")),
        "diff_sha256": hashlib.sha256(case.diff_text.encode("utf-8")).hexdigest(),
        "complete": True,
    }
    records = (
        [summary]
        + [
            {"record_type": "entry", "index": index, "entry": json_facts(entry)}
            for index, entry in enumerate(content.entries)
        ]
        + [
            {
                "record_type": "text",
                "sequence": index,
                "text": case.diff_text[offset : offset + 1900],
            }
            for index, offset in enumerate(range(0, len(case.diff_text), 1900))
        ]
    )
    expected = b"".join(canonical(record) + b"\n" for record in records)
    document = build_product_git_action_review(core, verified.diff, checkpoint=lambda: None)
    assert type(document) is ProductGitActionReviewDocument
    encoded = encode_product_git_action_review(document, checkpoint=lambda: None)
    assert encoded == expected
    assert decode_product_git_action_review(expected, checkpoint=lambda: None) == document
    assert "".join(chunk.text for chunk in document.chunks) == case.diff_text
    assert tuple(entry.entry for entry in document.entries) == content.entries
    assert document.summary.call == core.call and document.summary.call is not core.call
    assert document.summary.call.arguments is not core.call.arguments


def test_review_decode_rejects_duplicate_defaults_noncanonical_order_and_bad_containers(
    complete_review,
):
    """完整同义 JSON 仍须拒绝；缺省不能在重编码时默默补成新声明。"""
    _case, _verified, document = complete_review
    body = encode_product_git_action_review(document, checkpoint=lambda: None)
    lines = body.splitlines(keepends=True)
    first = json.loads(lines[0])
    missing = dict(first)
    del missing["complete"]
    duplicate = lines[0].replace(b"{", b'{"complete":true,', 1)
    altered = [
        body.decode(),
        bytearray(body),
        body[:-1],
        body.replace(b"\n", b"\r\n"),
        b" " + body,
        duplicate + b"".join(lines[1:]),
        canonical(missing) + b"\n" + b"".join(lines[1:]),
        lines[0] + b"".join(reversed(lines[1:])),
        lines[0] + lines[1] + lines[1] + b"".join(lines[2:]),
        body + b"{}\n",
        b"\xff\n",
    ]
    for candidate in altered:
        with pytest.raises(KernelError) as caught:
            decode_product_git_action_review(candidate, checkpoint=lambda: None)
        assert caught.value.code == "git_action_review_invalid"


def test_review_encoder_rejects_subclasses_extras_containers_and_broken_nested_hash(
    complete_review,
):
    """Pydantic 实例快速路径不能容忍实际子类、隐藏字段或列表冒充 tuple。"""
    _case, _verified, document = complete_review

    class Subdocument(ProductGitActionReviewDocument):
        pass

    subclass = Subdocument.model_validate_json(canonical(json_facts(document)), strict=True)
    extra = document.model_copy(deep=True)
    object.__setattr__(extra, "hidden", "正文不应出现在异常里")
    bad_chunk = document.chunks[0].model_copy(update={"text": "恶意正文"})
    candidates = [
        subclass,
        extra,
        document.model_copy(update={"entries": list(document.entries)}),
        document.model_copy(update={"chunks": (bad_chunk,)}),
        document.model_copy(update={"chunks": tuple(reversed(document.chunks)) + document.chunks}),
        document.model_copy(
            update={"summary": document.summary.model_copy(update={"file_count": 2})}
        ),
    ]
    for candidate in candidates:
        with pytest.raises(KernelError) as caught:
            encode_product_git_action_review(candidate, checkpoint=lambda: None)
        assert caught.value.code == "git_action_review_invalid"


def test_review_builder_rejects_diff_type_target_and_full_text_hash_mismatch(complete_review):
    case, verified, _document = complete_review
    diff = verified.diff
    altered = [
        object(),
        replace(diff, content=object()),
        replace(diff, content=replace(diff.content, text=diff.content.text + "恶意正文")),
        replace(diff, content=replace(diff.content, utf8_bytes=diff.content.utf8_bytes + 1)),
        replace(diff, content=replace(diff.content, sha256="f" * 64)),
    ]
    for candidate in altered:
        with pytest.raises(KernelError) as caught:
            build_product_git_action_review(case.core, candidate, checkpoint=lambda: None)
        assert caught.value.code == "git_action_review_invalid"


def test_review_public_codec_preserves_midoperation_control_exception_identity(complete_review):
    case, verified, document = complete_review
    body = encode_product_git_action_review(document, checkpoint=lambda: None)
    markers = [
        ValueError("仅测试控制"),
        TypeError("仅测试控制"),
        TurnCancelled(),
        KernelError("action_review_limit", "仅测试控制"),
        UpstreamCheckpointError(KernelError("control", "仅测试控制")),
    ]
    for marker in markers:
        for operation in (
            lambda check: build_product_git_action_review(
                case.core, verified.diff, checkpoint=check
            ),
            lambda check: encode_product_git_action_review(document, checkpoint=check),
            lambda check: decode_product_git_action_review(body, checkpoint=check),
        ):
            count = 0

            def check(_marker=marker):
                nonlocal count
                count += 1
                if count == 8:
                    raise _marker

            with pytest.raises(type(marker)) as caught:
                operation(check)
            assert caught.value is marker and count == 8


def test_review_entry_limit_is_256_and_chunks_reuse_original_3000_contract(complete_review):
    _case, _verified, document = complete_review
    entries = [
        {"record_type": "entry", "index": index, "entry": json_facts(document.entries[0].entry)}
        for index in range(256)
    ]
    expanded = review_document_like(document, "完整展示\n", entries=entries)
    body = encode_product_git_action_review(expanded, checkpoint=lambda: None)
    assert len(decode_product_git_action_review(body, checkpoint=lambda: None).entries) == 256
    assert type(expanded.chunks[0]) is WorkspaceActionReviewChunk
    entries.append({**entries[-1], "index": 256})
    with pytest.raises(ValueError):
        review_document_like(document, "完整展示\n", entries=entries)
    original = WorkspaceActionReviewChunk(sequence=0, text="中" * 3000)
    assert len(original.text) == 3000
    with pytest.raises(ValueError):
        WorkspaceActionReviewChunk(sequence=0, text="中" * 3001)


def test_review_actual_page_budget_accepts_50_and_rejects_51_within_one_mib(complete_review):
    """使用真实逐 LF/UTF8 裁切，而不是假定每页固定200条。"""
    _case, _verified, document = complete_review
    for count in (50, 51):
        text = "\x01" * (3000 * count)
        chunks = [
            {"record_type": "text", "sequence": index, "text": "\x01" * 3000}
            for index in range(count)
        ]
        candidate = review_document_like(document, text, chunks=chunks)
        raw = b"".join(
            canonical(record) + b"\n"
            for record in (
                json_facts(candidate.summary),
                *map(json_facts, candidate.entries),
                *chunks,
            )
        )
        assert len(raw) < 1024 * 1024
        lines = raw.decode().splitlines()
        offset, pages = 0, []
        while offset < len(lines):
            page, end = paginate_artifact_lines(lines, offset, 200)
            assert end > offset and len(page.encode()) <= 24 * 1024
            pages.append(page)
            offset = end
        assert len(pages) == count
        if count == 50:
            assert encode_product_git_action_review(candidate, checkpoint=lambda: None) == raw
        else:
            with pytest.raises(KernelError) as caught:
                encode_product_git_action_review(candidate, checkpoint=lambda: None)
            assert caught.value.code == "action_review_limit"


def test_review_rejects_total_bytes_records_and_single_record_lf_overflow(complete_review):
    _case, _verified, document = complete_review
    too_large = review_document_like(document, "中" * 400000)
    with pytest.raises(KernelError) as caught:
        encode_product_git_action_review(too_large, checkpoint=lambda: None)
    assert caught.value.code == "action_review_limit"
    for candidate in (b"{}\n" * 10001, b'"' + b"x" * (24 * 1024 - 1) + b'"\n'):
        with pytest.raises(KernelError):
            decode_product_git_action_review(candidate, checkpoint=lambda: None)


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
async def test_actual_producer_publishes_same_artifacts_with_signed_approval_backref(
    tmp_path, config, monkeypatch, fmt
):
    """原真实 Session、MAC 发布与 SDK 分页回读；不批准或执行默认 Git 业务。"""

    async def inspect(scenario):
        async def pending(actual):
            approval = next(
                item.content
                for item in actual.turn.items
                if type(item.content) is TrustedActionApprovalRequestContent
            )
            assert approval.presentation == "patch_batch"
            reference = approval.diff_artifact
            assert reference is not None
            assert actual.artifacts is scenario.client.transport.server.service.runtime._artifacts
            page = await scenario.client.read_artifact(
                actual.thread.thread_id, reference.artifact_id
            )
            body = page.text.encode("utf-8")
            assert page.next_offset is None and page.artifact.artifact_id == reference.artifact_id
            assert page.artifact.sha256 == reference.sha256
            assert hashlib.sha256(body).hexdigest() == reference.sha256
            document = decode_product_git_action_review(body, checkpoint=lambda: None)
            assert document.summary.core_fingerprint == actual.preparer.core.fingerprint
            assert document.summary.call == actual.call
            assert (
                "".join(chunk.text for chunk in document.chunks)
                == actual.preparer.diff.content.text
            )
            assert (
                tuple(entry.entry for entry in document.entries)
                == actual.preparer.diff.content.entries
            )

        await pending_actual_review(scenario, monkeypatch, pending, with_provider=True)

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=fmt, continuous=True
    )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
async def test_actual_sdk_continuous_patches_create_signed_pending_git_review(
    tmp_path, config, monkeypatch, fmt
):
    """实际 SDK/Kernel 保存 pending Git 调用，不批准或执行 Git 外部写入。"""

    async def inspect(scenario):
        async def pending(actual):
            assert type(scenario.session) is SQLiteSessionStore
            assert type(actual.artifacts) is SQLiteArtifactStore
            assert actual.artifacts.session is scenario.session
            assert type(actual.route) is ActionRouteSnapshotV2
            assert actual.route.state == "pending_approval"
            assert actual.turn.status == "waiting_approval"
            assert actual.call.tool == "git_checkpoint" and actual.call.requires_approval
            core = actual.preparer.core
            assert type(core) is ProductGitDeliveryCoreV2
            assert type(core.baseline.source) is ProductGitDeliverySourceV2
            assert len(core.baseline.source.patches) == 2
            assert core.store_id == scenario.session._publication._store_id
            assert core.key_id == scenario.session._publication._key_id
            assert core.call == actual.call
            assert actual.route.plan.resources[0].attributes_sha256 == core.fingerprint
            assert core.baseline.source.workspace == actual.route.plan.execution.workspace
            assert "-before\n+final\n" in actual.preparer.diff.content.text
            assert len(actual.preparer.diff.content.entries) == len(scenario.selected_paths)
            history = await scenario.session.authenticated_thread_history(
                actual.thread.thread_id, cancel=CancelToken(), deadline=monotonic() + 60
            )
            assert history.thread == actual.thread

        await pending_actual_review(scenario, monkeypatch, pending)

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=fmt, continuous=True
    )


async def test_actual_sdk_reads_all_git_pages_and_rejects_original_bound_approval(
    tmp_path, config, monkeypatch
):
    """完整多页真实读取及原拒绝关联，不以批准后假执行冒充业务交付。"""
    from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
    from harnessix.protocol.contracts import ApprovalRespondParams, PublicApprovalDecision
    from tests.agent.helpers import answer
    from tests.product_config.test_product_rollback_sdk import wait_turn
    from tests.support import git_user_observation as support

    original = support._proposal

    def proposal(root, **kwargs):
        value = original(root, **kwargs)
        return WorkspacePatchInput(
            files=tuple(
                item.model_copy(update={"content": "完整材料" * 1000 + "\n"})
                for item in value.files
            )
        )

    monkeypatch.setattr(support, "_proposal", proposal)

    async def inspect(scenario):
        async def pending(actual):
            approval = next(
                item.content
                for item in actual.turn.items
                if type(item.content) is TrustedActionApprovalRequestContent
            )
            ref, offset, pages, parts = approval.diff_artifact, 0, 0, []
            while True:
                page = await scenario.client.read_artifact(
                    actual.thread.thread_id, ref.artifact_id, offset=offset, limit=200
                )
                assert page.artifact.sha256 == ref.sha256 and page.offset == offset
                assert len(page.text.encode()) <= 24 * 1024
                parts.append(page.text)
                pages += 1
                if page.next_offset is None:
                    break
                assert page.next_offset == offset + page.text.count("\n")
                offset = page.next_offset
                assert pages <= 50
            assert 1 < pages <= 50
            encoded = "".join(parts).encode()
            assert len(encoded) == ref.size_bytes
            assert encoded.count(b"\n") == ref.records
            assert hashlib.sha256(encoded).hexdigest() == ref.sha256
            document = decode_product_git_action_review(encoded, checkpoint=lambda: None)
            assert (
                "".join(chunk.text for chunk in document.chunks)
                == actual.preparer.diff.content.text
            )
            scenario.bundle.steps = (scenario.bundle.steps[0], answer("交付已拒绝"))
            await scenario.client.respond_approval(
                ApprovalRespondParams(
                    request_id="reject-git-review",
                    thread_id=actual.thread.thread_id,
                    turn_id=actual.turn.turn_id,
                    approval_id=approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=PublicApprovalDecision(outcome="rejected", actor="reviewer"),
                )
            )
            await wait_turn(scenario.client, actual.thread.thread_id, "completed")
            assert actual.scenario.router.status(approval.plan_id).state == "denied"

        await pending_actual_review(scenario, monkeypatch, pending, with_provider=True)

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)
