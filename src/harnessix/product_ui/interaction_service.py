"""领域交互的Artifact验证与Agent Protocol命令适配。"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Final
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interactions import (
    ApprovalBinding,
    ApprovalEvidence,
    ApprovalEvidenceStatus,
    CancelTurnIntent,
    RespondApprovalIntent,
    RespondQuestionIntent,
    SteerTurnIntent,
    active_turn_control,
    approval_review,
    pending_question,
)
from harnessix.product_ui.projection import ProductViewState
from harnessix.product_ui.session import (
    ConnectionPhase,
    PreparedClientCommand,
    RecoverableAgentSession,
)
from harnessix.protocol.contracts import (
    MAX_PROTOCOL_TEXT_CHARS,
    ApprovalRespondParams,
    PublicApprovalDecision,
    PublicArtifactRef,
    QuestionRespondParams,
)
from harnessix.sdk import AgentClient, AgentSDKError

ARTIFACT_PAGE_LIMIT: Final = 200
MAX_ARTIFACT_PAGES: Final = 50
ARTIFACT_READ_TIMEOUT_SECONDS: Final = 5.0
LOCAL_APPROVAL_ACTOR: Final = "harnessix-code-local-user"


class _EvidenceFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _validate_reference(reference: PublicArtifactRef) -> None:
    if not reference.complete or reference.format != "jsonl/v1":
        raise _EvidenceFailure("diff_unavailable")


async def _read_artifact(
    session: RecoverableAgentSession,
    thread_id: UUID,
    reference: PublicArtifactRef,
) -> tuple[str, int]:
    _validate_reference(reference)

    async def read(client: AgentClient) -> tuple[str, int]:
        initialized = client.initialized
        if (
            initialized is None
            or not initialized.capabilities.artifact_pages
            or "artifact/read" not in initialized.capabilities.methods
        ):
            raise _EvidenceFailure("diff_unavailable")
        offset = 0
        records = 0
        size_bytes = 0
        chunks: list[str] = []
        for _ in range(MAX_ARTIFACT_PAGES):
            page = await client.read_artifact(
                thread_id,
                reference.artifact_id,
                offset=offset,
                limit=ARTIFACT_PAGE_LIMIT,
            )
            if page.artifact != reference:
                raise _EvidenceFailure("artifact_reference_changed")
            if page.offset != offset:
                raise _EvidenceFailure("artifact_pagination_stalled")
            if page.text and not page.text.endswith("\n"):
                raise _EvidenceFailure("artifact_integrity_failed")
            page_records = page.text.count("\n")
            page_bytes = len(page.text.encode("utf-8"))
            chunks.append(page.text)
            records += page_records
            size_bytes += page_bytes
            if (
                page_records > ARTIFACT_PAGE_LIMIT
                or records > reference.records
                or size_bytes > reference.size_bytes
            ):
                raise _EvidenceFailure("artifact_integrity_failed")
            if page.next_offset is None:
                break
            if page_records == 0 or page.next_offset != offset + page_records:
                raise _EvidenceFailure("artifact_pagination_stalled")
            offset = page.next_offset
        else:
            raise _EvidenceFailure("artifact_pagination_stalled")
        text = "".join(chunks)
        body = text.encode("utf-8")
        if (
            records != reference.records
            or len(body) != reference.size_bytes
            or hashlib.sha256(body).hexdigest() != reference.sha256
        ):
            raise _EvidenceFailure("artifact_integrity_failed")
        return text, records

    return await session.execute_query(read)


async def _respond_approval(
    session: RecoverableAgentSession,
    view: ProductViewState | None,
    evidence: ApprovalEvidence | None,
    intent: RespondApprovalIntent,
) -> ProductViewState:
    review = approval_review(view, evidence)
    if review.pending.binding != intent.binding:
        raise ProductUIError("approval_stale", "审批身份已经变化")
    if intent.outcome not in {"approved", "rejected"}:
        raise ProductUIError("approval_decision_invalid", "审批决定无效")
    if intent.reason is not None and len(intent.reason) > 2000:
        raise ProductUIError("approval_decision_invalid", "审批原因超过长度上限")
    if intent.outcome == "approved" and not review.approve_allowed:
        raise ProductUIError("approval_evidence_required", "批准前必须获得完整变更证据")
    command = session.prepare_command()

    async def respond(client: AgentClient, prepared: PreparedClientCommand) -> None:
        await client.respond_approval(
            ApprovalRespondParams(
                request_id=prepared.request_id,
                thread_id=intent.binding.thread_id,
                turn_id=intent.binding.turn_id,
                approval_id=intent.binding.approval_id,
                fingerprint=intent.binding.fingerprint,
                decision=PublicApprovalDecision(
                    outcome=intent.outcome,
                    actor=LOCAL_APPROVAL_ACTOR,
                    reason=intent.reason,
                ),
            )
        )

    await session.execute_prepared(command, respond)
    return await session.hydrate_thread(intent.binding.thread_id)


async def _respond_question(
    session: RecoverableAgentSession,
    view: ProductViewState | None,
    intent: RespondQuestionIntent,
) -> ProductViewState:
    pending = pending_question(view)
    if pending is None or pending.binding != intent.binding:
        raise ProductUIError("question_stale", "问题身份已经变化")
    if type(intent.answer) is not str or not intent.answer.strip() or len(intent.answer) > 4000:
        raise ProductUIError("question_answer_invalid", "回答为空或超过长度上限")
    command = session.prepare_command()

    async def respond(client: AgentClient, prepared: PreparedClientCommand) -> None:
        await client.respond_question(
            QuestionRespondParams(
                request_id=prepared.request_id,
                thread_id=intent.binding.thread_id,
                turn_id=intent.binding.turn_id,
                question_id=intent.binding.question_id,
                answer=intent.answer.strip(),
            )
        )

    await session.execute_prepared(command, respond)
    return await session.hydrate_thread(intent.binding.thread_id)


async def _cancel_turn(
    session: RecoverableAgentSession,
    view: ProductViewState | None,
    intent: CancelTurnIntent,
) -> ProductViewState:
    control = active_turn_control(view)
    if control is None or control.binding != intent.binding or not control.can_cancel:
        raise ProductUIError("turn_control_stale", "当前Turn已经不可取消")
    command = session.prepare_command()

    async def cancel(client: AgentClient, prepared: PreparedClientCommand) -> None:
        await client.cancel_turn(
            intent.binding.thread_id,
            intent.binding.turn_id,
            request_id=prepared.request_id,
        )

    await session.execute_prepared(command, cancel)
    return await session.hydrate_thread(intent.binding.thread_id)


async def _steer_turn(
    session: RecoverableAgentSession,
    view: ProductViewState | None,
    intent: SteerTurnIntent,
) -> ProductViewState:
    control = active_turn_control(view)
    if control is None or control.binding != intent.binding or not control.can_steer:
        raise ProductUIError("turn_control_stale", "当前Turn已经不可补充输入")
    if (
        type(intent.text) is not str
        or not intent.text.strip()
        or len(intent.text) > MAX_PROTOCOL_TEXT_CHARS
    ):
        raise ProductUIError("steering_invalid", "补充输入为空或超过长度上限")
    command = session.prepare_command()

    async def steer(client: AgentClient, prepared: PreparedClientCommand) -> None:
        await client.steer_turn(
            intent.binding.thread_id,
            intent.binding.turn_id,
            intent.text,
            request_id=prepared.request_id,
        )

    await session.execute_prepared(command, steer)
    return await session.hydrate_thread(intent.binding.thread_id)


class InteractionService:
    """在Controller单Actor内复核交互身份并调用受控Session边界。"""

    def __init__(self, session: RecoverableAgentSession) -> None:
        self._session = session

    async def load_approval_evidence(
        self,
        view: ProductViewState | None,
        binding: ApprovalBinding,
    ) -> ApprovalEvidence:
        review = approval_review(view, None)
        if review.pending.binding != binding:
            raise ProductUIError("approval_stale", "审批身份已经变化")
        current = review.evidence
        reference = review.pending.diff_artifact
        if current.status is not ApprovalEvidenceStatus.REQUIRED or reference is None:
            return current
        try:
            async with asyncio.timeout(ARTIFACT_READ_TIMEOUT_SECONDS):
                text, records = await _read_artifact(self._session, binding.thread_id, reference)
        except TimeoutError:
            return ApprovalEvidence(
                binding,
                ApprovalEvidenceStatus.UNAVAILABLE,
                artifact_id=reference.artifact_id,
                error_code="artifact_read_timeout",
            )
        except _EvidenceFailure as error:
            return ApprovalEvidence(
                binding,
                ApprovalEvidenceStatus.UNAVAILABLE,
                artifact_id=reference.artifact_id,
                error_code=error.code,
            )
        except AgentSDKError:
            if self._session.connection.phase is ConnectionPhase.BROKEN:
                raise
            return ApprovalEvidence(
                binding,
                ApprovalEvidenceStatus.UNAVAILABLE,
                artifact_id=reference.artifact_id,
                error_code="diff_unavailable",
            )
        return ApprovalEvidence(
            binding,
            ApprovalEvidenceStatus.READY,
            artifact_id=reference.artifact_id,
            text=text,
            sha256=reference.sha256,
            records=records,
        )

    async def respond_approval(
        self,
        view: ProductViewState | None,
        evidence: ApprovalEvidence | None,
        intent: RespondApprovalIntent,
    ) -> ProductViewState:
        return await _respond_approval(self._session, view, evidence, intent)

    async def respond_question(
        self,
        view: ProductViewState | None,
        intent: RespondQuestionIntent,
    ) -> ProductViewState:
        return await _respond_question(self._session, view, intent)

    async def cancel_turn(
        self,
        view: ProductViewState | None,
        intent: CancelTurnIntent,
    ) -> ProductViewState:
        return await _cancel_turn(self._session, view, intent)

    async def steer_turn(
        self,
        view: ProductViewState | None,
        intent: SteerTurnIntent,
    ) -> ProductViewState:
        return await _steer_turn(self._session, view, intent)
