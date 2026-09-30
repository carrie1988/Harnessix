"""受监督进程：签名、原子发布并验证Process Owner事实回执。"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Literal, Self, overload
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, model_validator

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import (
    ProcessOutputObservation,
    ProcessStopReason,
    SupervisionContract,
)
from harnessix.tools.contracts import Revision

MAX_OWNER_RECEIPT_BYTES = 64 * 1024
MAX_RAW_PROCESS_OUTPUT_BYTES = 2**63 - 1
_WINDOWS_RECEIPT_READ_DELAYS = (0.0, 0.002, 0.01, 0.05, 0.1, 0.25, 0.5)


class RawProcessOutputObservation(SupervisionContract):
    """私有原始流观察，仅认证数量、摘要和结束事实，不保留正文。"""

    model_config = ConfigDict(revalidate_instances="always")
    observed_bytes: int = Field(ge=0, le=MAX_RAW_PROCESS_OUTPUT_BYTES)
    sha256: Revision = Field(min_length=64, max_length=64)
    eof: bool

    @model_validator(mode="after")
    def consistent_empty_output(self) -> Self:
        if self.observed_bytes == 0 and self.sha256 != hashlib.sha256(b"").hexdigest():
            raise ValueError("空Process原始输出摘要不一致")
        return self


class _ProcessOwnerReceiptFacts(SupervisionContract):
    # 版本占位保持原v1字段顺序；具体子类各自约束Literal，不相互覆写。
    spec_version: str
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
        return self._terminal_shape(started)

    def _terminal_shape(self, started: bool) -> Self:
        """终态检查与运行态分离；保持原v1字段、失败规则和序列化字节。"""
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


class ProcessOwnerReceipt(_ProcessOwnerReceiptFacts):
    spec_version: Literal["harnessix.process-owner-receipt/v1"] = (
        "harnessix.process-owner-receipt/v1"
    )


class ProcessOwnerReceiptV2(_ProcessOwnerReceiptFacts):
    spec_version: Literal["harnessix.process-owner-receipt/v2"] = (
        "harnessix.process-owner-receipt/v2"
    )
    raw_stdout: RawProcessOutputObservation
    raw_stderr: RawProcessOutputObservation


OwnerReceipt = ProcessOwnerReceipt | ProcessOwnerReceiptV2


def parse_owner_receipt(body: bytes | str) -> OwnerReceipt:
    """显式按版本解析私有回执；仅校验合同，不代替身份与MAC验证。"""
    encoded = body.encode("utf-8") if isinstance(body, str) else body
    if not encoded or len(encoded) > MAX_OWNER_RECEIPT_BYTES:
        raise ValueError("Process owner回执字节数无效")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("Process owner回执必须是JSON对象")
    version = payload.get("spec_version")
    if version == "harnessix.process-owner-receipt/v1":
        return ProcessOwnerReceipt.model_validate_json(encoded)
    if version == "harnessix.process-owner-receipt/v2":
        return ProcessOwnerReceiptV2.model_validate_json(encoded)
    raise ValueError("Process owner回执版本无效")


def _canonical_payload(receipt: OwnerReceipt) -> bytes:
    payload = receipt.model_dump(mode="json", exclude={"mac"}, warnings="error")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def owner_receipt_mac(receipt: OwnerReceipt, owner_token: Revision) -> str:
    try:
        key = bytes.fromhex(owner_token)
    except ValueError:
        raise KernelError("process_owner_token_invalid", "Process owner token无效") from None
    if len(key) != 32:
        raise KernelError("process_owner_token_invalid", "Process owner token无效")
    return hmac.new(key, _canonical_payload(receipt), hashlib.sha256).hexdigest()


@overload
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
    raw_stdout: None = None,
    raw_stderr: None = None,
) -> ProcessOwnerReceipt: ...


@overload
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
    raw_stdout: RawProcessOutputObservation,
    raw_stderr: RawProcessOutputObservation,
) -> ProcessOwnerReceiptV2: ...


@overload
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
    raw_stdout: RawProcessOutputObservation | None,
    raw_stderr: RawProcessOutputObservation | None,
) -> OwnerReceipt: ...


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
    raw_stdout: RawProcessOutputObservation | None = None,
    raw_stderr: RawProcessOutputObservation | None = None,
) -> OwnerReceipt:
    if (raw_stdout is None) != (raw_stderr is None):
        raise KernelError("process_owner_receipt_invalid", "Process owner原始双流观察不完整")
    facts = dict(
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
    unsigned: OwnerReceipt
    if raw_stdout is not None and raw_stderr is not None:
        unsigned = ProcessOwnerReceiptV2.model_validate(
            {**facts, "raw_stdout": raw_stdout, "raw_stderr": raw_stderr}
        )
    else:
        unsigned = ProcessOwnerReceipt.model_validate(facts)
    return unsigned.model_copy(update={"mac": owner_receipt_mac(unsigned, owner_token)})


def verify_owner_receipt(
    receipt: OwnerReceipt,
    *,
    owner_token: Revision,
    process_id: UUID,
    owner_identity: Revision | None = None,
) -> OwnerReceipt:
    try:
        checked = parse_owner_receipt(receipt.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_owner_receipt_invalid", "Process owner回执无效") from None
    if (
        checked.process_id != process_id
        or (owner_identity is not None and checked.owner_identity != owner_identity)
        or not hmac.compare_digest(checked.mac, owner_receipt_mac(checked, owner_token))
    ):
        raise KernelError("process_owner_receipt_invalid", "Process owner回执绑定无效")
    return checked


def _read_owner_receipt_once(path: Path) -> OwnerReceipt:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        with ExitStack() as resources:
            if os.name == "nt":
                from harnessix.processes.windows_receipt import open_owner_receipt

                descriptor = resources.enter_context(open_owner_receipt(path))
            else:
                descriptor = os.open(path, flags)
                resources.callback(os.close, descriptor)
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
    except KernelError as error:
        if error.code == "process_owner_receipt_changed":
            raise
        raise ValueError("Process owner回执文件绑定无效") from None
    if len(body) != info.st_size:
        raise ValueError
    return parse_owner_receipt(body)


def _is_windows_sharing_error(error: OSError) -> bool:
    """识别Win32或CRT投影的Windows瞬时共享/访问冲突。"""

    return getattr(error, "winerror", None) in {5, 32} or (
        os.name == "nt" and isinstance(error, PermissionError) and error.errno == errno.EACCES
    )


def read_owner_receipt(
    path: Path,
    *,
    owner_token: Revision,
    process_id: UUID,
    owner_identity: Revision | None = None,
) -> OwnerReceipt:
    receipt: OwnerReceipt | None = None
    for index, delay in enumerate(_WINDOWS_RECEIPT_READ_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            receipt = _read_owner_receipt_once(path)
            break
        except FileNotFoundError:
            raise KernelError("process_owner_receipt_missing", "Process owner回执不存在") from None
        except KernelError as error:
            if error.code != "process_owner_receipt_changed":
                raise
            if index < len(_WINDOWS_RECEIPT_READ_DELAYS) - 1:
                continue
            failure = KernelError("process_owner_receipt_invalid", "Process owner回执损坏")
            failure.add_note(f"receipt_snapshot_changed:attempt={index + 1}")
            raise failure from None
        except OSError as error:
            final_attempt = index == len(_WINDOWS_RECEIPT_READ_DELAYS) - 1
            if _is_windows_sharing_error(error) and not final_attempt:
                continue
            failure = KernelError("process_owner_receipt_invalid", "Process owner回执损坏")
            failure.add_note(
                "receipt_io="
                f"{getattr(error, 'winerror', None) or 0}:{error.errno or 0}:"
                f"attempt={index + 1}"
            )
            raise failure from None
        except (ValidationError, ValueError, TypeError):
            failure = KernelError("process_owner_receipt_invalid", "Process owner回执损坏")
            failure.add_note("receipt_stage=content_or_file_binding")
            raise failure from None
    assert receipt is not None
    return verify_owner_receipt(
        receipt,
        owner_token=owner_token,
        process_id=process_id,
        owner_identity=owner_identity,
    )


def write_owner_receipt(path: Path, receipt: OwnerReceipt) -> None:
    """原子发布当前回执；调用方必须先持久化回执引用的输出前缀。"""
    try:
        body = receipt.model_dump_json(warnings="error").encode("utf-8")
        if len(body) > MAX_OWNER_RECEIPT_BYTES:
            raise KernelError("process_owner_receipt_invalid", "Process owner回执超过字节上限")
        body = parse_owner_receipt(body).model_dump_json(warnings="error").encode("utf-8")
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_owner_receipt_invalid", "Process owner回执无效") from None
    if os.name == "nt":
        from harnessix.processes.windows_receipt import publish_owner_receipt

        try:
            publish_owner_receipt(path, body)
        except (KernelError, OSError, ValueError) as error:
            failure = KernelError("process_owner_receipt_write_failed", "Process owner回执写入失败")
            if isinstance(error, OSError):
                failure.add_note(
                    f"receipt_publish_io={getattr(error, 'winerror', None) or 0}:{error.errno or 0}"
                )
            raise failure from None
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
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
