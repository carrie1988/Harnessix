"""审批过期时客户端续跑同一Turn，按时限失败收尾（Beta Issue #39）；终端界面与exec共用。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.protocol.contracts import ApprovalRespondParams, PublicApprovalDecision
from harnessix.sdk.approval_response import respond_approval_or_close_expired
from harnessix.sdk.errors import AgentSDKError


class _Client:
    def __init__(self, error: str | None) -> None:
        self.error = error
        self.resumed: list[tuple[object, object, str]] = []

    async def respond_approval(self, params: ApprovalRespondParams) -> None:
        if self.error is not None:
            raise AgentSDKError(self.error, "拒绝")

    async def resume_turn(self, thread_id: object, turn_id: object, *, request_id: str) -> None:
        self.resumed.append((thread_id, turn_id, request_id))


def _params() -> ApprovalRespondParams:
    return ApprovalRespondParams(
        request_id="cli-approval-1",
        thread_id=uuid4(),
        turn_id=uuid4(),
        approval_id=uuid4(),
        fingerprint="a" * 64,
        decision=PublicApprovalDecision(outcome="approved", actor="test"),
    )


async def test_a_decision_in_time_is_submitted_without_resuming() -> None:
    client = _Client(None)
    assert await respond_approval_or_close_expired(client, _params()) is True
    assert client.resumed == []


async def test_an_expired_approval_resumes_the_same_turn_once() -> None:
    client, params = _Client("approval_expired"), _params()
    assert await respond_approval_or_close_expired(client, params) is False
    assert client.resumed == [(params.thread_id, params.turn_id, "cli-approval-1-expired")]


@pytest.mark.parametrize("code", ["approval_closed", "approval_mismatch", "approval_not_found"])
async def test_other_rejections_are_not_turned_into_a_resume(code: str) -> None:
    client = _Client(code)
    with pytest.raises(AgentSDKError) as caught:
        await respond_approval_or_close_expired(client, _params())
    assert caught.value.code == code and client.resumed == []
