"""可信Hook扩展：汇总并导出受支持的公共入口，不承载运行时编排。"""

from harnessix.hooks.contracts import (
    HookActionInput,
    HookActionOutput,
    HookDefinition,
    HookDispatch,
    HookDispatchResult,
    HookMatcher,
    HookRegistrySnapshot,
    HookRunEvent,
    HookRunPlan,
    HookRunSnapshot,
    HookTrustGrant,
    build_hook_definition,
    build_hook_dispatch,
    build_hook_trust_grant,
)
from harnessix.hooks.runtime import HookRuntime
from harnessix.hooks.store import SQLiteHookStore

__all__ = [
    "HookActionInput",
    "HookActionOutput",
    "HookDefinition",
    "HookDispatch",
    "HookDispatchResult",
    "HookMatcher",
    "HookRegistrySnapshot",
    "HookRunEvent",
    "HookRunPlan",
    "HookRunSnapshot",
    "HookRuntime",
    "HookTrustGrant",
    "SQLiteHookStore",
    "build_hook_definition",
    "build_hook_dispatch",
    "build_hook_trust_grant",
]
