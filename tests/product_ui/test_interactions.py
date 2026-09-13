from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import harnessix.product_ui.interaction_service as service_module
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_service import InteractionService
from harnessix.product_ui.interactions import (
    ApprovalEvidenceStatus,
    RespondApprovalIntent,
    active_turn_control,
    approval_review,
    interaction_snapshot,
    pending_approval,
    pending_question,
    retained_approval_evidence,
)
from harnessix.product_ui.projection import ProductViewState, ProjectedItem, cold_product_view
from harnessix.product_ui.session import ConnectionPhase, ProductConnection
from harnessix.protocol.contracts import (
    ArtifactPageResult,
    InitializeResult,
    ProtocolLimits,
    PublicApprovalRequestContent,
    PublicArtifactRef,
    PublicBudget,
    PublicItem,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicToolCallContent,
    PublicUsage,
    ServerCapabilities,
    ServerInfo,
    ThreadView,
    TurnView,
)
from harnessix.sdk import AgentSDKError

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _view(
    status: str,
    contents: tuple[object, ...],
    *,
    thread_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> ProductViewState:
    resolved_thread = thread_id or uuid4()
    resolved_turn = turn_id or uuid4()
    turn = TurnView(
        turn_id=resolved_turn,
        request_id="turn-request",
        status=status,
        budget=PublicBudget(
            max_steps=20,
            max_tokens=4096,
            timeout_seconds=60.0,
            max_output_chars=100_000,
            max_tool_calls_per_step=8,
        ),
        usage=PublicUsage(input_tokens=13, output_tokens=8, total_tokens=21),
        model_steps=1,
        created_at=NOW,
    )
    thread = ThreadView(
        thread_id=resolved_thread,
        workspace="/workspace",
        cursor=len(contents),
        active_turn_id=resolved_turn,
        turn_count=1,
        latest_turn=turn,
        created_at=NOW,
        updated_at=NOW,
    )
    state = cold_product_view(thread)
    items = tuple(
        ProjectedItem(
            item=PublicItem(
                item_id=uuid4(),
                status=(
                    "started" if isinstance(content, PublicApprovalRequestContent) else "completed"
                ),
                content=content,  # type: ignore[arg-type]
            ),
            turn_id=resolved_turn,
            first_cursor=index,
            last_cursor=index,
            final=not isinstance(content, PublicApprovalRequestContent),
        )
        for index, content in enumerate(contents, 1)
    )
    return replace(state, durable_cursor=len(items), items=items)


def _approval_view(
    *,
    approval_type: str = "patch",
    reference: PublicArtifactRef | None = None,
) -> ProductViewState:
    call_id = uuid4()
    return _view(
        "waiting_approval",
        (
            PublicToolCallContent(
                call_id=call_id,
                tool="workspace.patch",
                tool_version="1",
                effect_class="idempotent_write",
                arguments={"path": "src/app.py", "replacement": "print('ok')"},
                requires_approval=True,
            ),
            PublicApprovalRequestContent(
                approval_type=approval_type,  # type: ignore[arg-type]
                approval_id=uuid4(),
                call_id=call_id,
                request_fingerprint="a" * 64,
                policy_version="policy-v1",
                diff_artifact=reference,
            ),
        ),
    )


def _artifact(text: str, *, sha256: str | None = None) -> PublicArtifactRef:
    body = text.encode()
    return PublicArtifactRef(
        artifact_id=uuid4(),
        sha256=sha256 or hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        records=text.count("\n"),
        format="jsonl/v1",
        complete=True,
        expires_at=NOW + timedelta(hours=1),
    )


class _ArtifactClient:
    def __init__(self, pages: tuple[ArtifactPageResult, ...]) -> None:
        self.pages = pages
        self.calls: list[tuple[UUID, UUID, int, int]] = []
        self.initialized = InitializeResult(
            server_info=ServerInfo(version="test"),
            capabilities=ServerCapabilities(methods=("artifact/read",), artifact_pages=True),
            limits=ProtocolLimits(),
        )

    async def read_artifact(
        self,
        thread_id: UUID,
        artifact_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> ArtifactPageResult:
        self.calls.append((thread_id, artifact_id, offset, limit))
        return self.pages[len(self.calls) - 1]


class _ArtifactSession:
    def __init__(self, client: _ArtifactClient) -> None:
        self.client = client
        self.connection = ProductConnection(1, ConnectionPhase.READY)
        self.query_calls = 0
        self.command_calls = 0

    async def execute_query(self, operation):
        self.query_calls += 1
        return await operation(self.client)

    def prepare_command(self):
        self.command_calls += 1
        raise AssertionError("只读证据加载不能分配Command ID")


def test_interaction_projection_binds_approval_question_control_and_unknown_cost() -> None:
    approval_view = _approval_view()
    approval = pending_approval(approval_view)
    assert approval is not None
    assert approval.binding.thread_id == approval_view.thread.thread_id
    assert approval.binding.turn_id == approval_view.current_turn.turn_id  # type: ignore[union-attr]
    assert approval.arguments_json == (
        '{\n  "path": "src/app.py",\n  "replacement": "print(\'ok\')"\n}'
    )
    review = approval_review(approval_view, None)
    assert review.evidence.status is ApprovalEvidenceStatus.INLINE
    assert review.approve_allowed

    question_id, call_id = uuid4(), uuid4()
    question_view = _view(
        "waiting_input",
        (
            PublicQuestionRequestContent(
                question_id=question_id,
                call_id=call_id,
                question="选择部署环境",
                options=("测试", "生产"),
            ),
        ),
    )
    question = pending_question(question_view)
    assert question is not None and question.options == ("测试", "生产")
    snapshot = interaction_snapshot(question_view)
    assert snapshot.usage_cost is not None
    assert snapshot.usage_cost.total_tokens == 21
    assert snapshot.usage_cost.max_tokens == 4096
    assert snapshot.usage_cost.cost_status == "unknown"
    assert snapshot.usage_cost.cost_reason == "price_not_exposed"
    assert snapshot.turn_control is not None and snapshot.turn_control.can_steer


def test_answered_question_and_changed_approval_identity_are_not_reused() -> None:
    question_id, call_id = uuid4(), uuid4()
    answered = _view(
        "waiting_input",
        (
            PublicQuestionRequestContent(
                question_id=question_id,
                call_id=call_id,
                question="继续吗",
            ),
            PublicQuestionAnswerContent(
                question_id=question_id,
                call_id=call_id,
                answer="继续",
            ),
        ),
    )
    with pytest.raises(ProductUIError, match="待回答问题不唯一"):
        pending_question(answered)

    first = _approval_view()
    evidence = approval_review(first, None).evidence
    changed = _approval_view()
    assert retained_approval_evidence(changed, evidence) is None


def test_turn_controls_follow_explicit_runtime_state_allowlists() -> None:
    finalizing = active_turn_control(_view("finalizing", ()))
    cancelling = active_turn_control(_view("cancelling", ()))
    completed = active_turn_control(_view("completed", ()))
    assert finalizing is not None and finalizing.can_cancel and not finalizing.can_steer
    assert cancelling is not None and not cancelling.can_cancel and not cancelling.can_steer
    assert completed is not None and not completed.can_cancel and not completed.can_steer


async def test_artifact_evidence_reads_all_pages_without_allocating_command() -> None:
    text = '{"line":1}\n{"line":2}\n'
    reference = _artifact(text)
    view = _approval_view(approval_type="patch_batch", reference=reference)
    binding = pending_approval(view).binding  # type: ignore[union-attr]
    client = _ArtifactClient(
        (
            ArtifactPageResult(
                artifact=reference,
                offset=0,
                text='{"line":1}\n',
                next_offset=1,
            ),
            ArtifactPageResult(
                artifact=reference,
                offset=1,
                text='{"line":2}\n',
                next_offset=None,
            ),
        )
    )
    session = _ArtifactSession(client)

    evidence = await InteractionService(session).load_approval_evidence(view, binding)  # type: ignore[arg-type]

    assert evidence.status is ApprovalEvidenceStatus.READY
    assert evidence.text == text and evidence.records == 2
    assert evidence.sha256 == reference.sha256
    assert session.query_calls == 1 and session.command_calls == 0
    assert [call[2:] for call in client.calls] == [(0, 200), (1, 200)]


@pytest.mark.parametrize(
    ("pages_factory", "expected_code"),
    [
        (
            lambda reference: (
                ArtifactPageResult(
                    artifact=reference.model_copy(update={"sha256": "f" * 64}),
                    offset=0,
                    text='{"line":1}\n',
                    next_offset=None,
                ),
            ),
            "artifact_reference_changed",
        ),
        (
            lambda reference: (
                ArtifactPageResult(
                    artifact=reference,
                    offset=0,
                    text='{"line":1}\n',
                    next_offset=0,
                ),
            ),
            "artifact_pagination_stalled",
        ),
        (
            lambda reference: (
                ArtifactPageResult(
                    artifact=reference,
                    offset=0,
                    text='{"line":1}\n',
                    next_offset=None,
                ),
            ),
            "artifact_integrity_failed",
        ),
    ],
)
async def test_artifact_evidence_fails_closed_with_stable_code(
    pages_factory,
    expected_code: str,
) -> None:
    advertised = '{"line":1}\n{"line":2}\n'
    reference = _artifact(advertised)
    view = _approval_view(approval_type="patch_batch", reference=reference)
    binding = pending_approval(view).binding  # type: ignore[union-attr]
    session = _ArtifactSession(_ArtifactClient(pages_factory(reference)))

    evidence = await InteractionService(session).load_approval_evidence(view, binding)  # type: ignore[arg-type]

    assert evidence.status is ApprovalEvidenceStatus.UNAVAILABLE
    assert evidence.error_code == expected_code
    assert not approval_review(view, evidence).approve_allowed


def test_batch_approval_without_diff_fails_closed() -> None:
    view = _approval_view(approval_type="patch_batch")
    review = approval_review(view, None)
    assert review.evidence.status is ApprovalEvidenceStatus.UNAVAILABLE
    assert review.evidence.error_code == "diff_unavailable"
    assert not review.approve_allowed


async def test_batch_approval_without_diff_is_rejected_before_command_allocation() -> None:
    view = _approval_view(approval_type="patch_batch")
    pending = pending_approval(view)
    assert pending is not None
    session = _ArtifactSession(_ArtifactClient(()))

    with pytest.raises(ProductUIError) as error:
        await InteractionService(session).respond_approval(  # type: ignore[arg-type]
            view,
            None,
            RespondApprovalIntent(pending.binding, outcome="approved"),
        )

    assert error.value.code == "approval_evidence_required"
    assert session.command_calls == 0


async def test_empty_and_fifty_page_artifacts_are_valid_boundaries() -> None:
    empty_ref = _artifact("")
    empty_view = _approval_view(approval_type="patch_batch", reference=empty_ref)
    empty_session = _ArtifactSession(
        _ArtifactClient(
            (
                ArtifactPageResult(
                    artifact=empty_ref,
                    offset=0,
                    text="",
                    next_offset=None,
                ),
            )
        )
    )
    empty = await InteractionService(empty_session).load_approval_evidence(  # type: ignore[arg-type]
        empty_view,
        pending_approval(empty_view).binding,  # type: ignore[union-attr]
    )
    assert empty.status is ApprovalEvidenceStatus.READY
    assert empty.text == "" and empty.records == 0

    text = "{}\n" * 50
    reference = _artifact(text)
    pages = tuple(
        ArtifactPageResult(
            artifact=reference,
            offset=index,
            text="{}\n",
            next_offset=None if index == 49 else index + 1,
        )
        for index in range(50)
    )
    view = _approval_view(approval_type="patch_batch", reference=reference)
    session = _ArtifactSession(_ArtifactClient(pages))
    evidence = await InteractionService(session).load_approval_evidence(  # type: ignore[arg-type]
        view,
        pending_approval(view).binding,  # type: ignore[union-attr]
    )
    assert evidence.status is ApprovalEvidenceStatus.READY
    assert len(session.client.calls) == 50


async def test_fifty_page_limit_fails_closed_when_artifact_has_more_pages() -> None:
    text = "{}\n" * 51
    reference = _artifact(text)
    pages = tuple(
        ArtifactPageResult(
            artifact=reference,
            offset=index,
            text="{}\n",
            next_offset=index + 1,
        )
        for index in range(50)
    )
    view = _approval_view(approval_type="patch_batch", reference=reference)
    session = _ArtifactSession(_ArtifactClient(pages))
    evidence = await InteractionService(session).load_approval_evidence(  # type: ignore[arg-type]
        view,
        pending_approval(view).binding,  # type: ignore[union-attr]
    )
    assert evidence.status is ApprovalEvidenceStatus.UNAVAILABLE
    assert evidence.error_code == "artifact_pagination_stalled"


async def test_missing_artifact_capability_and_timeout_do_not_allocate_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _artifact("{}\n")
    view = _approval_view(approval_type="patch_batch", reference=reference)
    binding = pending_approval(view).binding  # type: ignore[union-attr]
    unavailable_client = _ArtifactClient(())
    unavailable_client.initialized = unavailable_client.initialized.model_copy(
        update={
            "capabilities": ServerCapabilities(
                methods=("events/replay",),
                artifact_pages=False,
            )
        }
    )
    unavailable_session = _ArtifactSession(unavailable_client)
    unavailable = await InteractionService(unavailable_session).load_approval_evidence(  # type: ignore[arg-type]
        view, binding
    )
    assert unavailable.error_code == "diff_unavailable"
    assert unavailable_session.command_calls == 0

    class SlowClient(_ArtifactClient):
        async def read_artifact(self, *args, **kwargs):
            await asyncio.sleep(1)
            return await super().read_artifact(*args, **kwargs)

    monkeypatch.setattr(service_module, "ARTIFACT_READ_TIMEOUT_SECONDS", 0.001)
    slow_session = _ArtifactSession(
        SlowClient(
            (
                ArtifactPageResult(
                    artifact=reference,
                    offset=0,
                    text="{}\n",
                    next_offset=None,
                ),
            )
        )
    )
    timed_out = await InteractionService(slow_session).load_approval_evidence(  # type: ignore[arg-type]
        view, binding
    )
    assert timed_out.error_code == "artifact_read_timeout"
    assert slow_session.command_calls == 0


async def test_artifact_transport_failure_propagates_broken_connection() -> None:
    reference = _artifact("{}\n")
    view = _approval_view(approval_type="patch_batch", reference=reference)
    binding = pending_approval(view).binding  # type: ignore[union-attr]

    class BrokenSession:
        connection = ProductConnection(1, ConnectionPhase.BROKEN, "server_closed")

        async def execute_query(self, _operation):
            raise AgentSDKError("server_closed", "连接已关闭", retryable=True)

    with pytest.raises(AgentSDKError) as error:
        await InteractionService(BrokenSession()).load_approval_evidence(  # type: ignore[arg-type]
            view, binding
        )
    assert error.value.code == "server_closed"
