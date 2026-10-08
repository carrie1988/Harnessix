"""通过原 CAS 完整认证新旧领域、父闭包及物理壳，不授予执行权限。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionPlan, WorkspaceTransactionRecord
from harnessix.delivery.workspace_record_contracts import (
    MAX_STORED_RECORD_BYTES,
    WorkspacePlanReference,
    WorkspaceStoredRecord,
)
from harnessix.delivery.workspace_record_v3_contracts import WorkspaceStoredRecordV3
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.parent_closure_contracts import (
    WorkspaceParentChunkReference,
    WorkspaceParentClosureManifest,
    WorkspaceParentClosureReference,
)
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_ports import (
    WorkspacePureProgressFactory,
    protected_workspace_pure_progress,
)

WorkspaceRecordReference = (
    WorkspacePlanReference | WorkspaceParentClosureReference | WorkspaceParentChunkReference
)
_INVALID_DATA = (ValidationError, ValueError, TypeError, UnicodeError, RecursionError)


@dataclass(frozen=True, slots=True)
class DecodedWorkspaceRecord:
    """完整领域事实及实际验证过的物理引用，供Store和备份共同消费。"""

    record: WorkspaceTransactionRecord
    references: tuple[WorkspaceRecordReference, ...]


def encode_workspace_record(
    record: WorkspaceTransactionRecord,
    write_blob: Callable[[str, bytes], None],
    *,
    read_blob: Callable[[str], bytes] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """先耐久确认完整Plan，再产生满足原读限额的物理壳；不登记业务状态。"""

    check = _protected_checkpoint(checkpoint)
    try:
        return _encode(record, write_blob, read_blob, check)
    except UpstreamCheckpointError as error:
        raise error.error from None


def _encode(
    record: WorkspaceTransactionRecord,
    write_blob: Callable[[str, bytes], None],
    read_blob: Callable[[str], bytes] | None,
    check: Callable[[], None],
) -> str:
    check()
    checked = validate_workspace_record(record)
    try:
        body = checked.plan.model_dump_json(warnings="error").encode("utf-8")
        reference = WorkspacePlanReference(
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            fingerprint=checked.plan.fingerprint,
        )
        metadata = checked.model_dump(exclude={"spec_version", "plan"}, warnings="error")
        envelope_type = (
            WorkspaceStoredRecordV3
            if isinstance(checked, WorkspaceTransactionRecordV2)
            else WorkspaceStoredRecord
        )
        envelope = envelope_type(**metadata, plan_ref=reference)
        payload = envelope.model_dump_json(warnings="error")
        _bounded_payload(payload)
    except _INVALID_DATA:
        raise KernelError("delivery_record_invalid", "Workspace事务记录无效") from None
    if isinstance(checked, WorkspaceTransactionRecordV2) and read_blob is None:
        raise KernelError("delivery_record_invalid", "新Workspace事务缺少完整父历史读端口")
    # 写入端口必须是原Store.put_blob：已有Blob也须重新刷盘并完整回读。
    check()
    write_blob(reference.sha256, body)
    check()
    if isinstance(checked, WorkspaceTransactionRecordV2):
        assert read_blob is not None
        _closure_references(checked.plan.source, read_blob, check, write_blob=write_blob)
    check()
    return payload


def decode_workspace_record(
    payload: str,
    read_blob: Callable[[str], bytes],
    *,
    checkpoint: Callable[[], None] | None = None,
    pure_progress: WorkspacePureProgressFactory | None = None,
) -> DecodedWorkspaceRecord:
    """严格版本分派、完整CAS回读和领域摘要核验；未知版本不降级解析。"""

    owned_errors: list[UpstreamCheckpointError] = []

    def mark(error: BaseException) -> UpstreamCheckpointError:
        marked = UpstreamCheckpointError(error)
        owned_errors.append(marked)
        return marked

    def read(digest: str) -> bytes:
        try:
            return read_blob(digest)
        except KernelError as error:
            if error.code in {"delivery_blob_corrupt", "delivery_blob_invalid"}:
                raise
            raise mark(error) from None
        except BaseException as error:
            raise mark(error) from None

    check = _protected_checkpoint(checkpoint, mark_error=mark)
    try:
        if pure_progress is None:
            return _decode(payload, read, check)
        return _decode(
            payload,
            read,
            check,
            pure_progress=protected_workspace_pure_progress(pure_progress, mark_error=mark),
        )
    except UpstreamCheckpointError as error:
        if not any(error is marked for marked in owned_errors):
            raise
        raise error.error from None


def _decode(
    payload: str,
    read_blob: Callable[[str], bytes],
    check: Callable[[], None],
    *,
    pure_progress: WorkspacePureProgressFactory | None = None,
) -> DecodedWorkspaceRecord:
    check()
    try:
        _bounded_payload(payload)
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        version = value.get("spec_version", "harnessix.workspace-transaction-record/v1")
        if version == "harnessix.workspace-transaction-record/v1":
            return DecodedWorkspaceRecord(
                WorkspaceTransactionRecord.model_validate_json(payload, strict=True), ()
            )
        envelope_types: dict[str, type[WorkspaceStoredRecord]] = {
            "harnessix.workspace-stored-record/v2": WorkspaceStoredRecord,
            "harnessix.workspace-stored-record/v3": WorkspaceStoredRecordV3,
        }
        envelope_type = envelope_types.get(version)
        if envelope_type is None:
            raise ValueError
        envelope = envelope_type.model_validate_json(payload, strict=True)
    except _INVALID_DATA:
        raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None
    reference = envelope.plan_ref
    check()
    try:
        body = read_blob(reference.sha256)
    except KernelError as error:
        if error.code in {"delivery_blob_corrupt", "delivery_blob_invalid"}:
            raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None
        raise
    check()
    try:
        if (
            type(body) is not bytes
            or len(body) != reference.size
            or hashlib.sha256(body).hexdigest() != reference.sha256
        ):
            raise ValueError
        new_domain = isinstance(envelope, WorkspaceStoredRecordV3)
        plan_type = WorkspaceTransactionPlanV2 if new_domain else WorkspaceTransactionPlan
        plan = plan_type.model_validate_json(body, strict=True)
        if plan.fingerprint != reference.fingerprint:
            raise ValueError
        metadata = envelope.model_dump(
            mode="json",
            exclude={"spec_version", "domain_spec_version", "plan_ref"},
            warnings="error",
        )
        metadata["spec_version"] = envelope.domain_spec_version
        metadata["plan"] = plan.model_dump(mode="json", warnings="error")
        record_type = WorkspaceTransactionRecordV2 if new_domain else WorkspaceTransactionRecord
        record = record_type.model_validate_json(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            strict=True,
        )
    except _INVALID_DATA:
        raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None
    references: tuple[WorkspaceRecordReference, ...] = (reference,)
    if isinstance(record, WorkspaceTransactionRecordV2):
        if pure_progress is None:
            references += _closure_references(record.plan.source, read_blob, check)
        else:
            references += _closure_references(
                record.plan.source, read_blob, check, pure_progress=pure_progress
            )
    check()
    return DecodedWorkspaceRecord(record, references)


def validate_workspace_record(record: WorkspaceTransactionRecord) -> WorkspaceTransactionRecord:
    """使用实际新旧类型完整验真输入；不经旧 Record 丢弃新父引用。"""
    model = (
        WorkspaceTransactionRecordV2
        if isinstance(record, WorkspaceTransactionRecordV2)
        else WorkspaceTransactionRecord
    )
    try:
        return model.model_validate_json(record.model_dump_json(warnings="error"), strict=True)
    except _INVALID_DATA:
        raise KernelError("delivery_record_invalid", "Workspace事务记录无效") from None


def _closure_references(
    snapshot: WorkspaceSnapshotV2,
    read_blob: Callable[[str], bytes],
    check: Callable[[], None],
    *,
    write_blob: Callable[[str, bytes], None] | None = None,
    pure_progress: WorkspacePureProgressFactory | None = None,
) -> tuple[WorkspaceRecordReference, ...]:
    manifest_body = b""

    def read(digest: str) -> bytes:
        nonlocal manifest_body
        try:
            body = read_blob(digest)
        except KernelError as error:
            if error.code in {"delivery_blob_corrupt", "delivery_blob_invalid"}:
                raise
            raise UpstreamCheckpointError(error) from None
        if digest == snapshot.parent_closure.sha256:
            manifest_body = body
        if write_blob is not None:
            check()
            try:
                write_blob(digest, body)
            except BaseException as error:
                # 耐久写端口和父控制失败不归类为历史损坏。
                raise UpstreamCheckpointError(error) from None
            check()
        return body

    try:
        if pure_progress is None:
            read_workspace_parent_closure(snapshot, read, checkpoint=check)
        else:
            read_workspace_parent_closure(
                snapshot, read, checkpoint=check, pure_progress=pure_progress
            )
    except KernelError as error:
        if error.code == "workspace_closure_corrupt":
            raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None
        raise
    try:
        manifest = WorkspaceParentClosureManifest.model_validate_json(manifest_body, strict=True)
    except _INVALID_DATA:
        raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None
    return (snapshot.parent_closure, *manifest.chunks)


def _protected_checkpoint(
    checkpoint: Callable[[], None] | None,
    *,
    mark_error: Callable[[BaseException], UpstreamCheckpointError] = UpstreamCheckpointError,
) -> Callable[[], None]:
    def check() -> None:
        if checkpoint is not None:
            try:
                checkpoint()
            except BaseException as error:
                raise mark_error(error) from None

    return check


def _bounded_payload(payload: str) -> None:
    if type(payload) is not str or len(payload.encode("utf-8")) > MAX_STORED_RECORD_BYTES:
        raise ValueError
