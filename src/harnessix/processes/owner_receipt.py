"""受监督进程：签名、原子发布并验证Process Owner事实回执。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import (
    ProcessOutputObservation,
    ProcessStopReason,
    SupervisionContract,
)
from harnessix.tools.contracts import Revision

MAX_OWNER_RECEIPT_BYTES = 64 * 1024
_WINDOWS_RECEIPT_READ_DELAYS = (0.0, 0.002, 0.01, 0.05)


class ProcessOwnerReceipt(SupervisionContract):
    spec_version: Literal["harnessix.process-owner-receipt/v1"] = (
        "harnessix.process-owner-receipt/v1"
    )
    process_id: UUID
    owner_identity: Revision
    state: Literal["running", "exited", "failed", "unknown"]
    sequence: int = Field(ge=1)
    pid: int | None = Field(default=None, ge=2, le=2**32 - 1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = Field(default=None, ge=-(2**31), le=2**32 - 1)
    stop_reason: ProcessStopReason | None = None
    stdout: ProcessOutputObservation
    stderr: ProcessOutputObservation
    mac: Revision = Field(repr=False)

    @model_validator(mode="after")
    def receipt_shape(self) -> Self:
        running = self.state == "running"
        started = self.pid is not None and self.started_at is not None
        if (self.pid is None) != (self.started_at is None):
            raise ValueError("Process owner运行身份不完整")
        if running:
            if not started or self.finished_at is not None or self.stop_reason is not None:
                raise ValueError("运行中Process owner事实不一致")
            if self.returncode is not None:
                raise ValueError("运行中Process owner不能包含returncode")
            return self
        if self.finished_at is None or self.finished_at.tzinfo is None or self.stop_reason is None:
            raise ValueError("Process owner终态事实不完整")
        if self.state == "exited":
            if not started or self.returncode is None:
                raise ValueError("退出Process owner缺少运行事实")
        elif self.returncode is not None:
            raise ValueError("非退出Process owner不能包含returncode")
        if self.state == "failed" and (started or self.stop_reason != "launch_failed"):
            raise ValueError("Process owner启动失败事实不一致")
        if self.state == "unknown" and self.stop_reason not in {"cleanup_failed", "unknown"}:
            raise ValueError("Process owner未知终态原因无效")
        if self.started_at is not None:
            if self.started_at.tzinfo is None or self.finished_at < self.started_at:
                raise ValueError("Process owner时间顺序无效")
        return self


def _canonical_payload(receipt: ProcessOwnerReceipt) -> bytes:
    payload = receipt.model_dump(mode="json", exclude={"mac"}, warnings="error")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def owner_receipt_mac(receipt: ProcessOwnerReceipt, owner_token: Revision) -> str:
    try:
        key = bytes.fromhex(owner_token)
    except ValueError:
        raise KernelError("process_owner_token_invalid", "Process owner token无效") from None
    if len(key) != 32:
        raise KernelError("process_owner_token_invalid", "Process owner token无效")
    return hmac.new(key, _canonical_payload(receipt), hashlib.sha256).hexdigest()


def sign_owner_receipt(
    *,
    process_id: UUID,
    owner_identity: Revision,
    state: Literal["running", "exited", "failed", "unknown"],
    sequence: int,
    owner_token: Revision,
    stdout: ProcessOutputObservation,
    stderr: ProcessOutputObservation,
    pid: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    returncode: int | None = None,
    stop_reason: ProcessStopReason | None = None,
) -> ProcessOwnerReceipt:
    unsigned = ProcessOwnerReceipt(
        process_id=process_id,
        owner_identity=owner_identity,
        state=state,
        sequence=sequence,
        pid=pid,
        started_at=started_at,
        finished_at=finished_at,
        returncode=returncode,
        stop_reason=stop_reason,
        stdout=stdout,
        stderr=stderr,
        mac="0" * 64,
    )
    return unsigned.model_copy(update={"mac": owner_receipt_mac(unsigned, owner_token)})


def verify_owner_receipt(
    receipt: ProcessOwnerReceipt,
    *,
    owner_token: Revision,
    process_id: UUID,
    owner_identity: Revision | None = None,
) -> ProcessOwnerReceipt:
    try:
        checked = ProcessOwnerReceipt.model_validate_json(receipt.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_owner_receipt_invalid", "Process owner回执无效") from None
    if (
        checked.process_id != process_id
        or (owner_identity is not None and checked.owner_identity != owner_identity)
        or not hmac.compare_digest(checked.mac, owner_receipt_mac(checked, owner_token))
    ):
        raise KernelError("process_owner_receipt_invalid", "Process owner回执绑定无效")
    return checked


def _read_owner_receipt_once(path: Path) -> ProcessOwnerReceipt:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if info.st_size <= 0 or info.st_size > MAX_OWNER_RECEIPT_BYTES:
            raise ValueError
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise ValueError
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(body) != info.st_size:
        raise ValueError
    return ProcessOwnerReceipt.model_validate_json(body)


def _is_windows_sharing_error(error: OSError) -> bool:
    """只识别Windows原子替换与并发读取之间可瞬时恢复的共享冲突。"""

    return getattr(error, "winerror", None) in {5, 32}


def read_owner_receipt(
    path: Path,
    *,
    owner_token: Revision,
    process_id: UUID,
    owner_identity: Revision | None = None,
) -> ProcessOwnerReceipt:
    receipt: ProcessOwnerReceipt | None = None
    for index, delay in enumerate(_WINDOWS_RECEIPT_READ_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            receipt = _read_owner_receipt_once(path)
            break
        except FileNotFoundError:
            raise KernelError("process_owner_receipt_missing", "Process owner回执不存在") from None
        except OSError as error:
            final_attempt = index == len(_WINDOWS_RECEIPT_READ_DELAYS) - 1
            if _is_windows_sharing_error(error) and not final_attempt:
                continue
            raise KernelError("process_owner_receipt_invalid", "Process owner回执损坏") from None
        except (ValidationError, ValueError, TypeError):
            raise KernelError("process_owner_receipt_invalid", "Process owner回执损坏") from None
    assert receipt is not None
    return verify_owner_receipt(
        receipt,
        owner_token=owner_token,
        process_id=process_id,
        owner_identity=owner_identity,
    )


def write_owner_receipt(path: Path, receipt: ProcessOwnerReceipt) -> None:
    """原子发布当前回执；调用方必须先持久化回执引用的输出前缀。"""
    body = receipt.model_dump_json(warnings="error").encode("utf-8")
    if len(body) > MAX_OWNER_RECEIPT_BYTES:
        raise KernelError("process_owner_receipt_invalid", "Process owner回执超过字节上限")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except (OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise KernelError(
            "process_owner_receipt_write_failed", "Process owner回执写入失败"
        ) from None
