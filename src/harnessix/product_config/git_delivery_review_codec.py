"""正式Git Review严格规范JSONL；复用唯一编码与真实Artifact页裁切。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import Field, TypeAdapter

from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES
from harnessix.artifacts.sqlite import records
from harnessix.delivery.diff_content import WorkspaceDiffContent
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_tree_diff import GitTreeDiff
from harnessix.delivery.review_jsonl import ReviewJSONLLimitError, encode_review_records
from harnessix.delivery.trusted_action_contracts import WorkspaceActionReviewChunk
from harnessix.domain.artifact_pagination import (
    ARTIFACT_PAGE_LIMIT,
    MAX_ARTIFACT_PAGES,
    paginate_artifact_lines,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    _snapshot,
    snapshot_product_git_delivery_core_v2,
)
from harnessix.product_config.git_delivery_review_contracts import (
    ProductGitActionReviewDocument,
    ProductGitActionReviewEntry,
    ProductGitActionReviewSummary,
)

_CHUNK_CHARACTERS = 1900
_Record = Annotated[
    ProductGitActionReviewSummary | ProductGitActionReviewEntry | WorkspaceActionReviewChunk,
    Field(discriminator="record_type"),
]
_RECORD: TypeAdapter[_Record] = TypeAdapter(_Record)


def _invalid() -> KernelError:
    """只返回固定分类，不暴露路径、作者消息或解析器错误。"""
    return KernelError("git_action_review_invalid", "Git完整审阅材料不符合契约")


def _check_pages(body: bytes, checkpoint: Callable[[], None]) -> None:
    """同原24KiB/200条算法逐页计算；超过原50页整体拒绝。"""
    lines = records(body)
    offset = 0
    for _ in range(MAX_ARTIFACT_PAGES):
        checkpoint()
        _, end = paginate_artifact_lines(lines, offset, ARTIFACT_PAGE_LIMIT)
        if end == len(lines):
            return
        if end == offset:
            break
        offset = end
    raise KernelError("action_review_limit", "Git完整审阅材料超过审批分页上限")


def _encode(value: object, checkpoint: Callable[[], None]) -> bytes:
    """深层确切模型重建、原规范编码及原发布/实际读取预算全部通过才返回。"""
    document = _snapshot(value, ProductGitActionReviewDocument, checkpoint)
    body = encode_review_records(
        (document.summary, *document.entries, *document.chunks),
        checkpoint=checkpoint,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    try:
        _check_pages(body, checkpoint)
    except KernelError as error:
        if error.code == "artifact_invalid":
            raise KernelError("action_review_limit", "Git完整审阅材料超过Artifact上限") from None
        raise
    return body


def _build(
    core: object, diff: GitTreeDiff, checkpoint: Callable[[], None]
) -> ProductGitActionReviewDocument:
    """消费同次完整材料事实；不伪造事务，也不将数据摘要解释成业务认证。"""
    checked = snapshot_product_git_delivery_core_v2(core, checkpoint=checkpoint)
    checkpoint()
    if type(diff) is not GitTreeDiff or type(diff.content) is not WorkspaceDiffContent:
        raise _invalid()
    content = diff.content
    if content.utf8_bytes > MAX_ARTIFACT_BYTES:
        raise KernelError("action_review_limit", "Git完整审阅材料超过Artifact上限")
    if (content.sha256, content.utf8_bytes, diff.projection.root.object_id) != (
        checked.diff_sha256,
        checked.diff_bytes,
        checked.object_scope.roots.target_tree.object_id,
    ):
        raise _invalid()
    document = ProductGitActionReviewDocument(
        summary=ProductGitActionReviewSummary(
            core_fingerprint=checked.fingerprint,
            delivery_id=checked.delivery_id,
            call=checked.call,
            workspace_revision=checked.baseline.source.workspace.revision,
            base_commit_oid=checked.baseline.head_oid,
            target_tree_oid=checked.object_scope.roots.target_tree.object_id,
            file_count=len(content.entries),
            diff_utf8_bytes=content.utf8_bytes,
            diff_sha256=content.sha256,
        ),
        entries=tuple(
            ProductGitActionReviewEntry(index=index, entry=entry)
            for index, entry in enumerate(content.entries)
        ),
        chunks=tuple(
            WorkspaceActionReviewChunk(
                sequence=index, text=content.text[offset : offset + _CHUNK_CHARACTERS]
            )
            for index, offset in enumerate(range(0, len(content.text), _CHUNK_CHARACTERS))
        ),
    )
    # 最终严格重建断开可变Call.arguments别名，不能把原Core引用交给展示调用方。
    result = _snapshot(document, ProductGitActionReviewDocument, checkpoint)
    encode_product_git_action_review(result, checkpoint=checkpoint)
    return result


def _decode(body: object, checkpoint: Callable[[], None]) -> ProductGitActionReviewDocument:
    """只以LF分记录；规范重编码拒绝重复键、同义字节、缺省与错误顺序。"""
    checkpoint()
    if type(body) is not bytes:
        raise _invalid()
    error: BaseException | None = None

    def check() -> None:
        nonlocal error
        try:
            checkpoint()
        except BaseException as current:
            error = current
            raise

    try:
        decoded = []
        for line in records(body):
            check()
            decoded.append(_RECORD.validate_json(line))
        if not decoded or type(decoded[0]) is not ProductGitActionReviewSummary:
            raise _invalid()
        entries = tuple(item for item in decoded[1:] if type(item) is ProductGitActionReviewEntry)
        chunks = tuple(item for item in decoded[1:] if type(item) is WorkspaceActionReviewChunk)
        if tuple(decoded) != (decoded[0], *entries, *chunks):
            raise _invalid()
        document = ProductGitActionReviewDocument(
            summary=decoded[0], entries=entries, chunks=chunks
        )
        if encode_product_git_action_review(document, checkpoint=check) != body:
            raise _invalid()
        check()
        return document
    except (ValueError, TypeError, KernelError):
        if error is not None:
            raise error from None
        raise _invalid() from None


def _controlled[T](
    operation: Callable[[Callable[[], None]], T], checkpoint: Callable[[], None]
) -> T:
    """调用方控制异常优先保留原对象；仅组件自身解析失败映射固定公开分类。"""
    if type(checkpoint) is GitAuthenticationControl:
        # 三个调用方仅处理内存材料；Artifact 读取和发布仍在原完整控制边界外。
        with checkpoint.pure() as pure_check:
            return _controlled(operation, pure_check)
    error: BaseException | None = None

    def check() -> None:
        nonlocal error
        try:
            checkpoint()
        except BaseException as current:
            error = current
            raise

    try:
        return operation(check)
    except (ValueError, TypeError, AttributeError, KernelError) as current:
        if error is not None:
            raise error from None
        if isinstance(current, ReviewJSONLLimitError):
            raise KernelError("action_review_limit", "Git完整审阅材料超过Artifact上限") from None
        if isinstance(current, KernelError) and current.code == "action_review_limit":
            raise
        raise _invalid() from None


def encode_product_git_action_review(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """完整严格新合同、唯一JSONL及实际50页预检；异常不携带正文。"""
    return _controlled(lambda check: _encode(value, check), checkpoint)


def build_product_git_action_review(
    core: object, diff: GitTreeDiff, *, checkpoint: Callable[[], None]
) -> ProductGitActionReviewDocument:
    """将同次完整材料转换为新审阅，不新增认证、事务或执行权。"""
    return _controlled(lambda check: _build(core, diff, check), checkpoint)


def decode_product_git_action_review(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitActionReviewDocument:
    """完整规范重编码与严格递归；旧Workspace正文不可自动升级。"""
    return _controlled(lambda check: _decode(body, check), checkpoint)
