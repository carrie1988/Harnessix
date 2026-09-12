"""产品客户端核心：导出恢复状态合同与安全本地存储。"""

from harnessix.product_ui.contracts import (
    ClientCommandAllocation,
    ClientStateV1,
    ClientThreadCursor,
    workspace_identity_fingerprint,
)
from harnessix.product_ui.controller import (
    CloseReport,
    ControllerPhase,
    CreateThreadIntent,
    ProductController,
    ProductControllerState,
    ProductNotice,
    ReconnectIntent,
    RefreshThreadsIntent,
    SelectThreadIntent,
    StartRequest,
    SubmitPromptIntent,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.projection import (
    ProductViewState,
    ProjectedItem,
    TransientItemStream,
    TurnProjection,
    apply_events_next,
    apply_item_delta,
    apply_replay_page,
    cold_product_view,
    refresh_thread_snapshot,
)
from harnessix.product_ui.session import (
    ConnectionPhase,
    PreparedClientCommand,
    ProductConnection,
    RecoverableAgentSession,
)
from harnessix.product_ui.state_store import ClientStateStore

__all__ = [
    "ClientCommandAllocation",
    "ClientStateStore",
    "ClientStateV1",
    "ClientThreadCursor",
    "CloseReport",
    "ConnectionPhase",
    "ControllerPhase",
    "CreateThreadIntent",
    "PreparedClientCommand",
    "ProductConnection",
    "ProductController",
    "ProductControllerState",
    "ProductNotice",
    "ProductViewState",
    "ProductUIError",
    "ProjectedItem",
    "RecoverableAgentSession",
    "ReconnectIntent",
    "RefreshThreadsIntent",
    "SelectThreadIntent",
    "StartRequest",
    "SubmitPromptIntent",
    "TransientItemStream",
    "TurnProjection",
    "apply_events_next",
    "apply_item_delta",
    "apply_replay_page",
    "cold_product_view",
    "refresh_thread_snapshot",
    "workspace_identity_fingerprint",
]
