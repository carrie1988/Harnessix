"""受控崩溃子进程：加载既有计划执行一次真实写效果后硬退出；仅由Action恢复Soak启动。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from scripts.soak_action_recovery import CrashWriteExecutor, crash_definition


def main(argv: list[str]) -> int:
    """argv：plans库、audit库、Workspace、计划ID与效果标记路径；正常路径不返回。"""

    plans_path, audit_path, workspace, plan_id, marker = argv[1:6]
    root = Path(workspace)
    plans = SQLiteExecutionPlanStore(plans_path)
    audit = SQLiteActionAuditStore(audit_path)
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
    )
    router.register(crash_definition(CrashWriteExecutor(Path(marker))))
    asyncio.run(router.execute(UUID(plan_id)))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
