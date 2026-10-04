"""通过原私有CAS保存完整Plan，统一读取旧内嵌记录和新引用记录。"""

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


@dataclass(frozen=True, slots=True)
class DecodedWorkspaceRecord:
    """完整领域事实及实际验证过的物理引用，供Store和备份共同消费。"""

    record: WorkspaceTransactionRecord
    references: tuple[WorkspacePlanReference, ...]


def encode_workspace_record(
    record: WorkspaceTransactionRecord,
    write_blob: Callable[[str, bytes], None],
) -> str:
    """先耐久确认完整Plan，再产生满足原读限额的物理壳；不登记业务状态。"""

    try:
        checked = WorkspaceTransactionRecord.model_validate_json(
            record.model_dump_json(warnings="error"), strict=True
        )
        body = checked.plan.model_dump_json(warnings="error").encode("utf-8")
        reference = WorkspacePlanReference(
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            fingerprint=checked.plan.fingerprint,
        )
        metadata = checked.model_dump(exclude={"spec_version", "plan"}, warnings="error")
        envelope = WorkspaceStoredRecord(**metadata, plan_ref=reference)
        payload = envelope.model_dump_json(warnings="error")
        _bounded_payload(payload)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise KernelError("delivery_record_invalid", "Workspace事务记录无效") from None
    # 写入端口必须是原Store.put_blob：已有Blob也须重新刷盘并完整回读。
    write_blob(reference.sha256, body)
    return payload


def decode_workspace_record(
    payload: str,
    read_blob: Callable[[str], bytes],
) -> DecodedWorkspaceRecord:
    """严格版本分派、完整CAS回读和领域摘要核验；未知版本不降级解析。"""

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
        envelope = WorkspaceStoredRecord.model_validate_json(payload, strict=True)
        reference = envelope.plan_ref
        body = read_blob(reference.sha256)
        if (
            type(body) is not bytes
            or len(body) != reference.size
            or hashlib.sha256(body).hexdigest() != reference.sha256
        ):
            raise ValueError
        plan = WorkspaceTransactionPlan.model_validate_json(body, strict=True)
        if plan.fingerprint != reference.fingerprint:
            raise ValueError
        metadata = envelope.model_dump(
            mode="json",
            exclude={"spec_version", "domain_spec_version", "plan_ref"},
            warnings="error",
        )
        metadata["spec_version"] = envelope.domain_spec_version
        metadata["plan"] = plan.model_dump(mode="json", warnings="error")
        record = WorkspaceTransactionRecord.model_validate_json(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            strict=True,
        )
        return DecodedWorkspaceRecord(record, (reference,))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError, KernelError):
        raise KernelError("delivery_store_corrupt", "Workspace事务账本损坏") from None


def _bounded_payload(payload: str) -> None:
    if type(payload) is not str or len(payload.encode("utf-8")) > MAX_STORED_RECORD_BYTES:
        raise ValueError
