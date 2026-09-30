"""Owner回执的私有认证与Lease字段投影；不执行状态提交或进程控制。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.processes.owner_receipt import OwnerReceipt, read_owner_receipt
from harnessix.processes.supervision_contracts import ProcessLease


async def read_terminal_owner_receipt(
    lease: ProcessLease, directory: Path, *, accepted_sequence: int
) -> OwnerReceipt:
    """调用方持有原句柄锁；终态缓存不能替代原文件MAC和Lease身份事实。"""
    if lease.state != "exited":
        raise KernelError("process_owner_receipt_invalid", "Process缺少已知退出事实")
    receipt = await asyncio.to_thread(
        read_owner_receipt,
        directory / "receipt.json",
        owner_token=lease.owner_token,
        process_id=lease.process_id,
        owner_identity=lease.owner_identity,
    )
    fields = (
        "state",
        "owner_identity",
        "pid",
        "started_at",
        "finished_at",
        "returncode",
        "stop_reason",
        "stdout",
        "stderr",
    )
    if any(getattr(receipt, name) != getattr(lease, name) for name in fields) or (
        accepted_sequence and receipt.sequence != accepted_sequence
    ):
        raise KernelError("process_owner_receipt_invalid", "Process原回执与退出Lease不一致")
    return receipt


def lease_changes_from_receipt(current: ProcessLease, receipt: OwnerReceipt) -> dict[str, object]:
    """只投影原脱敏事实；raw和Owner序号不得混入Lease或替代Store的CAS序号。"""
    if receipt.state == "running":
        return {
            "state": "stopping" if current.state == "stopping" else "running",
            "sequence": current.sequence + 1,
            "owner_identity": receipt.owner_identity,
            "pid": receipt.pid,
            "started_at": receipt.started_at,
            "stdout": receipt.stdout,
            "stderr": receipt.stderr,
        }
    if receipt.state == "failed":
        return {
            "state": "failed",
            "sequence": current.sequence + 1,
            "finished_at": receipt.finished_at,
            "stop_reason": "launch_failed",
            "stdout": receipt.stdout,
            "stderr": receipt.stderr,
        }
    if receipt.state == "unknown":
        return {
            "state": "unknown",
            "sequence": current.sequence + 1,
            "owner_identity": receipt.owner_identity if receipt.pid is not None else None,
            "pid": receipt.pid,
            "started_at": receipt.started_at,
            "finished_at": receipt.finished_at,
            "stop_reason": receipt.stop_reason,
            "stdout": receipt.stdout,
            "stderr": receipt.stderr,
        }
    return {
        "state": "exited",
        "sequence": current.sequence + 1,
        "owner_identity": receipt.owner_identity,
        "pid": receipt.pid,
        "started_at": receipt.started_at,
        "finished_at": receipt.finished_at,
        "returncode": receipt.returncode,
        "stop_reason": receipt.stop_reason,
        "stdout": receipt.stdout,
        "stderr": receipt.stderr,
    }
