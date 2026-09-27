"""Process模型可见摘要合同：只接受有界终态元数据，不包含输出正文。"""

from __future__ import annotations

import hashlib
from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.processes.supervision_contracts import MAX_PROCESS_OUTPUT_BYTES, ProcessStopReason
from harnessix.tools.contracts import ReadContract, Revision


class PublicProcessStreamSummary(ReadContract):
    """公开观察摘要；持久流有上限，观察字节数可能因最后一批输出越过上限。"""

    observed_bytes: int = Field(ge=0)
    observed_sha256: Revision
    persisted_bytes: int = Field(ge=0, le=MAX_PROCESS_OUTPUT_BYTES)
    truncated: bool
    eof: bool

    @model_validator(mode="after")
    def consistent_prefix(self) -> Self:
        if (
            self.persisted_bytes > self.observed_bytes
            or self.truncated != (self.persisted_bytes < self.observed_bytes)
            or (
                self.observed_bytes == 0 and self.observed_sha256 != hashlib.sha256(b"").hexdigest()
            )
        ):
            raise ValueError("公开Process流摘要不一致")
        return self


class PublicProcessOutputSummary(ReadContract):
    """与既有public_output形状相同；验证使用原JSON，不重编码审计摘要。"""

    version: Literal["trusted-process-output/v1"]
    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    process_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    state: Literal["exited", "failed", "unknown"]
    returncode: int | None
    stop_reason: ProcessStopReason
    stdout: PublicProcessStreamSummary
    stderr: PublicProcessStreamSummary
    complete: bool

    @model_validator(mode="after")
    def truthful_terminal(self) -> Self:
        if (self.state == "exited") != (self.returncode is not None) or (
            self.complete
            and not all(item.eof and not item.truncated for item in (self.stdout, self.stderr))
        ):
            raise ValueError("公开Process终态摘要不一致")
        if self.state == "failed" and self.stop_reason != "launch_failed":
            raise ValueError("公开Process启动失败原因不一致")
        if self.state == "unknown" and self.stop_reason not in {
            "host_lost",
            "cleanup_failed",
            "unknown",
        }:
            raise ValueError("公开Process未知状态原因不一致")
        return self


class PublicEvalOutputSummary(PublicProcessOutputSummary):
    """Eval结论只允许从终态事实派生，测试不通过不是基础设施失败。"""

    passed: bool

    @model_validator(mode="after")
    def derived_test_result(self) -> Self:
        if self.passed != (
            self.state == "exited" and self.stop_reason == "exited" and self.returncode == 0
        ):
            raise ValueError("公开Eval结论与Process终态不一致")
        return self
