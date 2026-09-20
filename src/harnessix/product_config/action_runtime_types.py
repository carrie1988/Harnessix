"""产品Action Runtime内部类型，隔离恢复扫描与装配模块的循环依赖。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from harnessix.processes.supervision_contracts import ProcessLease


class ProductProcessSupervisor(Protocol):
    """恢复扫描只依赖Process Owner的只读清单与既有对账能力。"""

    def active_leases(self) -> tuple[ProcessLease, ...]: ...

    async def reconcile(self, process_id: UUID) -> ProcessLease: ...
