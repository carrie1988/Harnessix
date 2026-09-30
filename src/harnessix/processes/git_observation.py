"""宿主Git基准的私有结果；原始观察不进入普通Process或模型输出合同。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.contracts import ProcessResult, ProcessStream
from harnessix.processes.owner_receipt import RawProcessOutputObservation
from harnessix.tools.contracts import ReadToolError


def git_reader_binding(process_binding: str, for_delivery: bool) -> str:
    """原普通端口身份不变；交付端口显式区分原始统计的证明合同。"""
    return (
        canonical_digest({"process_binding": process_binding, "observation": "git-raw/v2"})
        if for_delivery
        else process_binding
    )


def validate_git_result(result: ProcessResult, *, repository_check: bool = False) -> None:
    """EOF只证明流结束；取消、期限、限额或非零退出均不能成为成功基准。"""
    if result.stop_reason == "cancelled":
        raise TurnCancelled
    if result.stop_reason == "timeout":
        raise ReadToolError("timeout")
    if (
        result.stop_reason != "exited"
        or result.returncode != 0
        or result.termination != "none"
        or not result.stdout.eof
        or not result.stderr.eof
    ):
        raise ReadToolError("not_found" if repository_check else "io_failed")


@dataclass(frozen=True, slots=True)
class GitBaselineReadResult:
    """原安全Process结果与双流raw统计分离，不保存原始正文、路径或认证密钥。"""

    result: ProcessResult = field(repr=False)
    raw_stdout: RawProcessOutputObservation = field(repr=False)
    raw_stderr: RawProcessOutputObservation = field(repr=False)

    def __post_init__(self) -> None:
        validate_git_result(self.result)
        if not self.raw_stdout.eof or not self.raw_stderr.eof:
            raise ReadToolError("io_failed")

    @classmethod
    def from_posix_capture(cls, result: ProcessResult) -> GitBaselineReadResult:
        """仅用于原POSIX capture；Windows脱敏ProcessStream不能据此推断raw。"""

        def raw(stream: ProcessStream) -> RawProcessOutputObservation:
            return RawProcessOutputObservation(
                observed_bytes=stream.observed_bytes,
                sha256=stream.observed_sha256,
                eof=stream.eof,
            )

        return cls(result, raw(result.stdout), raw(result.stderr))

    def full_stdout(self) -> bytes:
        """只有完整且与raw等长同摘要的安全正文才可用于元数据决策。"""
        validate_git_result(self.result)
        stream = self.result.stdout
        if stream.truncated:
            raise ReadToolError("limit_exceeded")
        body = stream.data()
        if (
            len(body) != self.raw_stdout.observed_bytes
            or hashlib.sha256(body).hexdigest() != self.raw_stdout.sha256
        ):
            raise KernelError("git_baseline_metadata_changed", "Git基准元数据正文与原始观察不一致")
        return body
