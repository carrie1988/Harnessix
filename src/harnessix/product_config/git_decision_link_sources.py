"""原审批证据的私有声明映射；普通事实不构成来源认证或执行许可。"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, ItemFinished, Thread
from harnessix.product_config.git_approval_history_projection import (
    OriginalGitApprovalHistory,
    interpret_git_approval_history,
)
from harnessix.product_config.git_approval_history_proof import ApprovalHistoryEvidence
from harnessix.product_config.git_decision_link_contracts import (
    GitSessionEventRef,
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDeniedLink,
)
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_proof import PreparedLinkEvidence
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.product_config.git_user_observation import _native_checkpointer
from harnessix.session.event_body_refs import EventBodyRef
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _changed() -> KernelError:
    """只报告固定结构错误，不携带源事件、SQL 或正文。"""
    return KernelError("git_approval_history_changed", "Git决定原来源无法核验")


def session_event_refs(
    history: AuthenticatedThreadHistory, *, checkpoint: Callable[[], None]
) -> dict[UUID, GitSessionEventRef]:
    """核对完整对应关系，摘要只取读取定位；不认证调用者构造的聚合。"""
    checkpoint()
    if (
        type(history) is not AuthenticatedThreadHistory
        or type(history.thread) is not Thread
        or type(history.thread.thread_id) is not UUID
        or type(history.events) is not tuple
        or type(history.body_refs) is not tuple
        or not history.events
        or len(history.events) != len(history.body_refs)
        or type(history.thread.sequence) is not int
        or history.thread.sequence != len(history.events)
    ):
        raise _changed()
    result: dict[UUID, GitSessionEventRef] = {}
    for sequence, (event, ref) in enumerate(zip(history.events, history.body_refs, strict=True), 1):
        checkpoint()
        if (
            type(event) is not AgentEvent
            or type(ref) is not EventBodyRef
            or type(ref.thread_id) is not UUID
            or type(ref.event_id) is not UUID
            or type(ref.sequence) is not int
            or type(event.thread_id) is not UUID
            or type(event.event_id) is not UUID
            or type(event.sequence) is not int
            or (ref.thread_id, ref.event_id, ref.sequence)
            != (event.thread_id, event.event_id, event.sequence)
            or event.thread_id != history.thread.thread_id
            or ref.sequence != sequence
            or type(ref.body_sha256) is not str
            or len(ref.body_sha256) != 64
            or any(character not in "0123456789abcdef" for character in ref.body_sha256)
            or event.event_id in result
        ):
            raise _changed()
        result[event.event_id] = GitSessionEventRef(
            event_id=event.event_id, sequence=event.sequence, digest=ref.body_sha256
        )
    checkpoint()
    return result


def build_git_decision_link_sources(
    evidence: ApprovalHistoryEvidence, *, checkpoint: Callable[[], None]
) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
    """原控制窗口内重建闭合声明，不装配 Reader/Writer 或发布认证事实。"""
    # 复用原控制异常载体，避免 Pydantic 把宿主 ValueError 当成数据错误。
    try:
        return _build(evidence, _native_checkpointer(checkpoint))
    except UpstreamCheckpointError as error:
        raise error.error from None


def _build(
    evidence: ApprovalHistoryEvidence, check: Callable[[], None]
) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
    check()
    if (
        type(evidence) is not ApprovalHistoryEvidence
        or type(evidence.materials) is not PreparedLinkEvidence
        or type(evidence.projection) is not OriginalGitApprovalHistory
        or type(evidence.materials.link) is not ProductGitPreparedLink
        or type(evidence.materials.route) is not ActionRouteSnapshotV2
        or type(evidence.route_events) is not tuple
    ):
        raise _changed()
    materials, projection = evidence.materials, evidence.projection
    prepared = materials.link
    refs = session_event_refs(materials.history, checkpoint=check)
    if (
        projection.state not in {"approved", "denied", "cancelled"}
        or evidence.approval is None
        or materials.route.plan != prepared.plan.route
        or materials.history.thread.thread_id != prepared.plan.core.thread_id
    ):
        raise _changed()
    # 不把普通投影标签当成来源语义；复用原完整 Reducer/Route 解释，不另写状态机。
    actual = interpret_git_approval_history(
        materials.history,
        prepared,
        materials.route,
        evidence.route_events,
        evidence.approval,
        checkpoint=check,
    )
    if actual != projection or actual.route_decision is None:
        raise _changed()
    originals = {event.event_id: event for event in materials.history.events}

    def locate(event: AgentEvent) -> GitSessionEventRef:
        check()
        if (
            type(event) is not AgentEvent
            or event.turn_id != prepared.plan.core.turn_id
            or originals.get(event.event_id) != event
        ):
            raise _changed()
        return refs[event.event_id]

    # 原 prepared Rows 已严格核对原规范正文；这里沿原算法求前驱正文索引。
    body = encode_product_git_prepared_link(prepared, checkpoint=check)
    values: dict[str, object] = {
        "plan": prepared.plan,
        "approval_request": prepared.approval,
        "request_event": locate(actual.request_event),
        "prepared_body_sha256": sha256(body).hexdigest(),
        "router_approval": evidence.approval,
        "route_decision_sequence": actual.route_decision.sequence,
        "route_decision_digest": actual.route_decision.digest,
    }
    kind: type[ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink]
    if actual.state == "cancelled":
        names = (
            "cancel_event",
            "approval_cancel_event",
            "call_result_event",
            "turn_terminal_event",
        )
        values.update(
            (name, locate(event)) for name, event in zip(names, actual.cancel_events, strict=True)
        )
        kind = ProductGitCancelledLink
    else:
        event = actual.decision_event
        assert event is not None and isinstance(event.payload, ItemFinished)
        values.update(session_decision=event.payload.content, decision_event=locate(event))
        kind = ProductGitApprovedLink if actual.state == "approved" else ProductGitDeniedLink
    check()
    result = kind.model_validate(values, context={"checkpoint": check})
    check()
    return result
