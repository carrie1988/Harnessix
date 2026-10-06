"""已知Git/Workspace审阅在原发布Guard的全文保护；不计为认证会话正控。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import (
    WorkspaceActionReviewDocument,
    WorkspaceActionReviewEntry,
    WorkspaceActionReviewSummary,
)
from harnessix.product_config.git_delivery_review_codec import (
    build_product_git_action_review,
    encode_product_git_action_review,
)
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from tests.artifacts.test_authenticated_body import SecretReference
from tests.support.git_delivery_review import material_case, review_document_like


@pytest.fixture
def body(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas") as store:
        case, verified = material_case(GitMaterialCAS(store), tmp_path)
        document = build_product_git_action_review(
            case.core, verified.diff, checkpoint=lambda: None
        )
        yield document


def scope(value):
    """固定合成凭据，不读取宿主环境或钥匙串。"""
    return SecretPublicationScope(
        (SecretReference(name="review", version="1"),),
        EnvironmentSecretProvider(
            (EnvironmentSecretSource("review", "1", "SYNTHETIC_REVIEW_KEY"),),
            environment={"SYNTHETIC_REVIEW_KEY": value},
        ),
    )


@pytest.mark.parametrize("version", ["git", "workspace"])
async def test_original_guard_reconstructs_cross_chunk_fulltext_for_current_scope(body, version):
    synthetic = "review-boundary-synthetic-value"
    text = "x" * (1900 - 8) + synthetic + "\n"
    document = review_document_like(body, text)
    encoded = encode_product_git_action_review(document, checkpoint=lambda: None)
    if version == "workspace":
        document = WorkspaceActionReviewDocument(
            summary=WorkspaceActionReviewSummary(
                transaction_id=uuid4(),
                plan_fingerprint=body.summary.core_fingerprint,
                workspace_revision=body.summary.workspace_revision,
                file_count=len(document.entries),
                diff_utf8_bytes=len(text.encode()),
                diff_sha256=document.summary.diff_sha256,
            ),
            entries=tuple(
                WorkspaceActionReviewEntry(index=item.index, entry=item.entry)
                for item in document.entries
            ),
            chunks=document.chunks,
        )
        encoded = document.to_jsonl()
    assert all(synthetic not in item.text for item in document.chunks)
    with scope(synthetic) as protection:
        guard = ArtifactPublicationGuard(protection)
        # 普通JSONL逐叶保护确实无法看见跨块值；完整Review用途必须额外重建。
        await guard.check_body(encoded, purpose="tool_result")
        with pytest.raises(KernelError) as caught:
            await guard.check_body(encoded, purpose="action_review")
        assert caught.value.code == "public_output_secret_leak"
    with scope("unrelated-synthetic-value") as protection:
        await ArtifactPublicationGuard(protection).check_body(encoded, purpose="action_review")


@pytest.mark.parametrize("change", ["sequence", "sha", "missing", "order"])
async def test_known_review_cannot_hide_incomplete_fulltext_from_public_guard(body, change):
    encoded = encode_product_git_action_review(body, checkpoint=lambda: None)
    records = [json.loads(line) for line in encoded.decode().split("\n")[:-1]]
    if change == "sequence":
        records[-1]["sequence"] += 1
    elif change == "sha":
        records[0]["diff_sha256"] = "f" * 64
    elif change == "missing":
        records.pop()
    else:
        records = [records[0], *reversed(records[1:])]
    invalid = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
    with scope("unrelated-synthetic-value") as protection:
        with pytest.raises(KernelError) as caught:
            await ArtifactPublicationGuard(protection).check_body(invalid, purpose="action_review")
        assert caught.value.code == "public_output_protection_failed"


async def test_original_generic_action_review_format_remains_readable():
    with scope("unrelated-synthetic-value") as protection:
        await ArtifactPublicationGuard(protection).check_body(
            b'{"view":"plan","complete":true}\n', purpose="action_review"
        )
