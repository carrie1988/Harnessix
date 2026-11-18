"""提交审批决定；Turn时限已过时把这一轮收尾为time_budget_exceeded。终端界面与无人值守客户端共用。

审批超过Turn截止时间后，服务端拒绝新的决定（`approval_expired`），这一轮仍停在等待审批：
事件里没有终态，`harnessix exec`的摘要也只剩错误码，没有会话与Turn信息（Beta Issue #39）。
服务端约定由客户端续跑同一Turn，续跑时把它记为`time_budget_exceeded`失败（与重启后的恢复相同），
这里在收到`approval_expired`后立即续跑，客户端随后照常跟随到终态。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from harnessix.protocol.contracts import ApprovalRespondParams
from harnessix.sdk.errors import AgentSDKError


class _ApprovalClient(Protocol):
    async def respond_approval(self, params: ApprovalRespondParams) -> object: ...

    async def resume_turn(self, thread_id: UUID, turn_id: UUID, *, request_id: str) -> object: ...


async def respond_approval_or_close_expired(
    client: _ApprovalClient, params: ApprovalRespondParams
) -> bool:
    """提交决定并返回True；审批已过期时续跑同一Turn使它以时限失败结束，返回False。"""

    try:
        await client.respond_approval(params)
    except AgentSDKError as error:
        if error.code != "approval_expired":
            raise
        await client.resume_turn(
            params.thread_id, params.turn_id, request_id=f"{params.request_id}-expired"
        )
        return False
    return True
