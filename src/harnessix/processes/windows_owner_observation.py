"""Windows Owner的pipe观察支持；二进制读取、进度与启动失败回执保持独立。"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from harnessix.processes.owner_output import CapturedProcessOutput
from harnessix.processes.owner_protocol import ProcessOwnerStart
from harnessix.processes.owner_receipt import OwnerReceipt, sign_owner_receipt


def configure_binary_output_reader(terminal: str, name: str, descriptor: int) -> None:
    """只调整原生pipe的输出FD，避免CRT换行/Ctrl-Z转换，不修改控制或ConPTY。"""
    if sys.platform == "win32" and terminal == "pipe" and name in {"stdout", "stderr"}:
        import msvcrt

        msvcrt.setmode(descriptor, os.O_BINARY)


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
    """只签调用方已结束的输出事实；无运行PID，pipe为v2，ConPTY保持v1。"""
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
