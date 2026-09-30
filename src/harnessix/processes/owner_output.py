"""受监督进程：持久捕获进程输出并生成有界完整性观察。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.processes.owner_protocol import ProcessOwnerStart
from harnessix.processes.owner_receipt import (
    MAX_RAW_PROCESS_OUTPUT_BYTES,
    OwnerReceipt,
    RawProcessOutputObservation,
    sign_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessOutputObservation
from harnessix.secrets.redaction import StreamingSecretRedactor


class CapturedProcessOutput:
    """原始流仅计量；脱敏流独立计量并按额度落盘。"""

    def __init__(self, path: Path, secrets: tuple[bytes, ...]) -> None:
        # CRT文本转换会让物理字节与Receipt摘要分叉；不能依赖宿主默认模式。
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._redactor = StreamingSecretRedactor(secrets)
        self._fd = os.open(path, flags, 0o600)
        self._raw_digest = hashlib.sha256()
        self._raw_observed = 0
        self._digest = hashlib.sha256()
        self._persisted_digest = hashlib.sha256()
        self._observed = 0
        self._persisted = 0
        self._closed = False
        self.eof = False

    @property
    def raw_observed(self) -> int:
        return self._raw_observed

    @property
    def observed(self) -> int:
        return self._observed

    @property
    def persisted(self) -> int:
        return self._persisted

    def feed(self, data: bytes, allowance: int) -> int:
        # 与脱敏器输入合同一致，拒绝输入不得污染原始统计。
        if self._closed:
            raise KernelError("secret_redaction_closed", "Secret脱敏器已经关闭")
        if type(data) is not bytes:
            raise KernelError("secret_redaction_failed", "Secret脱敏输入必须是bytes")
        observed = self._raw_observed + len(data)
        if observed > MAX_RAW_PROCESS_OUTPUT_BYTES:
            raise KernelError("process_output_limit_exceeded", "Process原始输出观察超过字节上限")
        self._raw_observed = observed
        self._raw_digest.update(data)
        return self._publish(self._redactor.feed(data), allowance)

    def finish(self, allowance: int, *, eof: bool) -> int:
        if self._closed:
            return 0
        emitted = self._publish(self._redactor.finish(), allowance)
        self.eof = eof
        self._closed = True
        return emitted

    def _publish(self, data: bytes, allowance: int) -> int:
        self._observed += len(data)
        self._digest.update(data)
        persisted = data[: max(0, allowance)]
        view = memoryview(persisted)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("short process output write")
            view = view[written:]
        self._persisted += len(persisted)
        self._persisted_digest.update(persisted)
        return len(data)

    def sync(self) -> None:
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def observation(self) -> ProcessOutputObservation:
        return ProcessOutputObservation(
            observed_bytes=self._observed,
            persisted_bytes=self._persisted,
            sha256=self._digest.hexdigest(),
            persisted_sha256=self._persisted_digest.hexdigest(),
            truncated=self._persisted < self._observed,
            eof=self.eof,
        )

    def raw_observation(self) -> RawProcessOutputObservation:
        return RawProcessOutputObservation(
            observed_bytes=self._raw_observed,
            sha256=self._raw_digest.hexdigest(),
            eof=self.eof,
        )


def capture_process_streams(
    root: Path, request: ProcessOwnerStart, protected: tuple[bytes, ...]
) -> tuple[CapturedProcessOutput, CapturedProcessOutput]:
    """双平台共用原捕获策略；第二流创建失败时回收第一流FD。"""
    values = (*protected, *(request.environment[name].encode() for name in request.secret_names))
    stdout = CapturedProcessOutput(root / "stdout.bin", values)
    try:
        return stdout, CapturedProcessOutput(root / "stderr.bin", values)
    except BaseException:
        stdout.close()
        raise


def output_position(
    stdout: CapturedProcessOutput, stderr: CapturedProcessOutput
) -> tuple[int, int, int, int, int, int]:
    """原始输入的新增进度不能因脱敏尾窗尚未发布而消失。"""
    return (
        stdout.raw_observed,
        stdout.observed,
        stdout.persisted,
        stderr.raw_observed,
        stderr.observed,
        stderr.persisted,
    )


def launch_failed_output_receipt(
    request: ProcessOwnerStart, stdout: CapturedProcessOutput, stderr: CapturedProcessOutput
) -> OwnerReceipt:
    """只签调用方已结束的输出事实；无运行PID，pipe为v2，PTY/ConPTY保持v1。"""
    return sign_owner_receipt(
        process_id=request.process_id,
        owner_identity=request.owner_identity,
        state="failed",
        sequence=1,
        owner_token=request.owner_token,
        finished_at=datetime.now(UTC),
        stop_reason="launch_failed",
        stdout=stdout.observation(),
        stderr=stderr.observation(),
        raw_stdout=stdout.raw_observation() if request.terminal == "pipe" else None,
        raw_stderr=stderr.raw_observation() if request.terminal == "pipe" else None,
    )


def output_limit_exceeded(
    request: ProcessOwnerStart, stdout: CapturedProcessOutput, stderr: CapturedProcessOutput
) -> bool:
    """pipe原始输入与保护后发布分别限额，EOF尾窗也不得越过原预算。"""
    return (
        request.terminal == "pipe"
        and stdout.raw_observed + stderr.raw_observed > request.output_bytes
    ) or stdout.observed + stderr.observed > request.output_bytes
