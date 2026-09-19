"""模型Context规划：构造有界模型历史并归档被替换的Tool Result。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Item,
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.artifacts.contracts import ArtifactOmittedField, ArtifactPage, ArtifactRef
from harnessix.context.tool_result_contracts import (
    TOOL_RESULT_OMISSION_VERSION,
    ModelHistoryInspection,
    ModelHistoryInspectionRecord,
    ToolResultArtifactBinding,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.tools.search_contracts import GlobOutput, GrepOutput

_PUBLIC_RESULT_FIELDS = {"outcome", "output", "error", "diff_artifact"}


@dataclass(frozen=True, slots=True)
class ModelHistoryArtifactReference:
    call_id: UUID
    binding: ToolResultArtifactBinding
    omitted_field: Literal["preview", "matches", "paths"] | None = None
    owner_thread_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PreparedModelHistory:
    history: tuple[Item, ...]
    inspection: ModelHistoryInspectionRecord
    new_decisions: tuple[ToolResultViewDecision, ...]
    references: tuple[ModelHistoryArtifactReference, ...]


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError):
        raise KernelError(
            "context_tool_result_invalid", "模型历史不能规范化为有限值UTF-8 JSON"
        ) from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def history_items(thread: Thread) -> tuple[Item, ...]:
    inherited = thread.fork_snapshot.items if thread.fork_snapshot is not None else ()
    local = tuple(
        item
        for turn in thread.turns
        for item in turn.items
        if item.status == ItemStatus.COMPLETED
        and isinstance(item.content, TextContent | ToolCallContent | ToolResultContent)
    )
    return (*inherited, *local)


def history_document(item: Item) -> str:
    data = item.model_dump(mode="json")
    if isinstance(item.content, ToolResultContent):
        data["content"]["output"] = item.content.output
    elif isinstance(item.content, ToolCallContent):
        data["content"]["arguments"] = item.content.arguments
    return _canonical(data).decode()


def _public_result(content: ToolResultContent) -> dict[str, Any]:
    data = content.model_dump(mode="json", include=_PUBLIC_RESULT_FIELDS)
    # Pydantic的JSON模式会把NaN/Infinity转成null；原始JSON值必须先经过有限值校验。
    data["output"] = content.output
    return data


def _artifact(value: object) -> ArtifactRef:
    try:
        return ArtifactRef.model_validate_json(_canonical(value))
    except (ValueError, TypeError):
        raise KernelError(
            "context_tool_result_artifact_invalid",
            "Tool Result包含不符合契约的Artifact引用",
        ) from None


def _bindings(
    content: ToolResultContent, call: ToolCallContent
) -> tuple[ToolResultArtifactBinding, ...]:
    found: list[ToolResultArtifactBinding] = []
    output = content.output
    if isinstance(output, dict) and "artifact" in output:
        if call.tool == "read_artifact":
            try:
                ArtifactPage.model_validate_json(_canonical(output))
            except ValueError:
                raise KernelError(
                    "context_tool_result_artifact_invalid", "Artifact分页结果不符合契约"
                ) from None
            purpose: Literal["tool_result", "process_output", "action_output", "artifact_page"] = (
                "artifact_page"
            )
        else:
            purpose = (
                "action_output"
                if content.trusted_action is not None
                else "process_output"
                if content.process is not None
                else "tool_result"
            )
        found.append(
            ToolResultArtifactBinding(
                purpose=purpose,
                artifact=_artifact(output["artifact"]),
            )
        )
    if content.diff_artifact is not None:
        found.append(
            ToolResultArtifactBinding(
                purpose=("action_review" if content.trusted_action is not None else "batch_effect"),
                artifact=content.diff_artifact,
            )
        )
    return tuple(found)


def _preview_metadata(preview: object) -> tuple[object, Literal["preview", "matches", "paths"]]:
    if isinstance(preview, dict):
        for field, model in (("matches", GrepOutput), ("paths", GlobOutput)):
            if field not in preview:
                continue
            try:
                model.model_validate_json(_canonical(preview))
            except ValidationError:
                continue
            return (
                {key: deepcopy(value) for key, value in preview.items() if key != field},
                cast(ArtifactOmittedField, field),
            )
    return None, "preview"


def _replacement(
    content: ToolResultContent,
    source_sha256: str,
    source_utf8_bytes: int,
    bindings: tuple[ToolResultArtifactBinding, ...],
) -> ToolResultContent:
    result_bindings = [binding for binding in bindings if binding.purpose == "tool_result"]
    if content.process is not None or content.patch is not None or content.patch_batch is not None:
        raise KernelError(
            "context_tool_result_unsupported",
            "Patch或Process Tool Result超过模型视图上限且没有全结果替换契约",
        )
    if len(result_bindings) != 1 or not isinstance(content.output, dict):
        raise KernelError(
            "context_tool_result_artifact_required",
            "超限Tool Result必须在首次提交时绑定完整Artifact",
        )
    binding = result_bindings[0]
    if set(content.output) != {"preview", "artifact"}:
        raise KernelError(
            "context_tool_result_unsupported",
            "超限Tool Result结构不能由完整结果Artifact安全替换",
        )
    if not binding.artifact.complete:
        raise KernelError(
            "context_tool_result_artifact_incomplete",
            "不完整Artifact不能替代超限Tool Result",
        )
    metadata, omitted_field = _preview_metadata(content.output["preview"])
    return content.model_copy(
        deep=True,
        update={
            "output": {
                "preview": metadata,
                "model_view": {
                    "spec_version": TOOL_RESULT_OMISSION_VERSION,
                    "reason": "inline_budget_exceeded",
                    "source_utf8_bytes": source_utf8_bytes,
                    "source_sha256": source_sha256,
                    "omitted_field": omitted_field,
                },
                "artifact": binding.artifact.model_dump(mode="json"),
            }
        },
    )


def _new_decision(
    item: Item, call: ToolCallContent, policy: ToolResultViewPolicy, *, legacy: bool
) -> tuple[ToolResultViewDecision, ToolResultContent]:
    assert isinstance(item.content, ToolResultContent)
    content = item.content
    source = _canonical(_public_result(content))
    source_sha256 = hashlib.sha256(source).hexdigest()
    bindings = _bindings(content, call)
    if len(source) <= policy.max_inline_utf8_bytes:
        view = content.model_copy(deep=True)
        strategy: Literal["inline", "artifact_reference"] = "inline"
        replacement_output = None
    else:
        if legacy:
            raise KernelError(
                "context_tool_result_decision_mismatch", "旧历史没有替换证据，不能改变已见模型前缀"
            )
        view = _replacement(content, source_sha256, len(source), bindings)
        strategy = "artifact_reference"
        replacement_output = view.output
    encoded_view = _canonical(_public_result(view))
    if len(encoded_view) > policy.max_inline_utf8_bytes:
        raise KernelError(
            "context_tool_result_unsupported",
            "Tool Result引用模型视图仍超过配置上限",
        )
    return (
        ToolResultViewDecision(
            item_id=item.item_id,
            call_id=content.call_id,
            strategy=strategy,
            limit_utf8_bytes=policy.max_inline_utf8_bytes,
            source_sha256=source_sha256,
            source_utf8_bytes=len(source),
            view_sha256=hashlib.sha256(encoded_view).hexdigest(),
            view_utf8_bytes=len(encoded_view),
            references=bindings,
            replacement_output=replacement_output,
        ),
        view,
    )


def _apply_decision(
    item: Item,
    call: ToolCallContent,
    decision: ToolResultViewDecision,
    policy: ToolResultViewPolicy,
) -> ToolResultContent:
    assert isinstance(item.content, ToolResultContent)
    content = item.content
    source = _canonical(_public_result(content))
    decision = ToolResultViewDecision.model_validate_json(decision.model_dump_json())
    bindings = _bindings(content, call)
    if (
        decision.item_id != item.item_id
        or decision.call_id != content.call_id
        or decision.source_sha256 != hashlib.sha256(source).hexdigest()
        or decision.source_utf8_bytes != len(source)
        or decision.references != bindings
    ):
        raise KernelError(
            "context_tool_result_decision_mismatch",
            "冻结的Tool Result模型视图决定与Session事实不一致",
        )
    view = (
        content.model_copy(deep=True)
        if decision.strategy == "inline"
        else content.model_copy(deep=True, update={"output": deepcopy(decision.replacement_output)})
    )
    if decision.strategy == "artifact_reference":
        assert isinstance(content.output, dict)
        metadata, omitted_field = _preview_metadata(content.output.get("preview"))
        assert isinstance(view.output, dict)
        omission = view.output["model_view"]
        if (
            content.patch is not None
            or content.patch_batch is not None
            or content.process is not None
            or set(content.output) != {"preview", "artifact"}
            or view.output["preview"] != metadata
            or not isinstance(omission, dict)
            or omission["omitted_field"] != omitted_field
        ):
            raise KernelError("context_tool_result_decision_mismatch", "替换决定改变了未归档字段")
    encoded = _canonical(_public_result(view))
    if (
        len(encoded) > policy.max_inline_utf8_bytes
        or len(encoded) != decision.view_utf8_bytes
        or hashlib.sha256(encoded).hexdigest() != decision.view_sha256
    ):
        raise KernelError(
            "context_tool_result_decision_mismatch",
            "冻结的Tool Result模型视图不满足当前边界或摘要校验",
        )
    return view


def prepare_model_history(
    thread: Thread,
    model_step: int,
    policy: ToolResultViewPolicy,
    *,
    decisions: tuple[ToolResultViewDecision, ...] | None = None,
) -> PreparedModelHistory:
    return prepare_model_history_items(
        thread,
        history_items(thread),
        model_step,
        policy,
        decisions=decisions,
        require_all_prior_decisions=True,
    )


def prepare_model_history_items(
    thread: Thread,
    source_history: tuple[Item, ...],
    model_step: int,
    policy: ToolResultViewPolicy,
    *,
    decisions: tuple[ToolResultViewDecision, ...] | None = None,
    require_all_prior_decisions: bool = False,
) -> PreparedModelHistory:
    """准备显式活动历史；调用方必须证明被省略的旧事实来自已发布窗口。"""
    if not source_history:
        raise KernelError("empty_transcript", "模型历史不能为空")
    if (
        len(source_history) > 8192
        or sum(len(history_document(i).encode()) for i in source_history) > 8_388_608
    ):
        raise KernelError("context_budget_exceeded", "模型历史超过8192项或8 MiB准备上限")
    prior = [
        *(thread.fork_snapshot.tool_result_view_decisions if thread.fork_snapshot else ()),
        *(decision for turn in thread.turns for decision in turn.tool_result_view_decisions),
    ]
    decisions_by_item = {decision.item_id: decision for decision in prior}
    if len(decisions_by_item) != len(prior):
        raise KernelError(
            "context_tool_result_decision_mismatch", "Tool Result模型视图决定身份重复"
        )
    legacy_ids = {
        item.item_id
        for turn in thread.turns
        if turn.model_steps > 0 and not turn.model_history_inspections
        for item in turn.items
    }
    supplied = {decision.item_id: decision for decision in decisions or ()}
    if len(supplied) != len(decisions or ()) or set(supplied) & set(decisions_by_item):
        raise KernelError("context_tool_result_decision_mismatch", "新模型视图决定重复")
    calls: dict[UUID, ToolCallContent] = {}
    settled: set[UUID] = set()

    prepared: list[Item] = []
    used: list[ToolResultViewDecision] = []
    created: list[ToolResultViewDecision] = []
    for item in source_history:
        if isinstance(item.content, ToolCallContent):
            if item.content.call_id in calls:
                raise KernelError("context_tool_result_decision_mismatch", "历史调用身份重复")
            calls[item.content.call_id] = item.content
        if not isinstance(item.content, ToolResultContent):
            prepared.append(item.model_copy(deep=True))
            continue
        call = calls.get(item.content.call_id)
        if call is None or item.content.call_id in settled:
            raise KernelError("context_tool_result_decision_mismatch", "历史结果缺少唯一前置调用")
        settled.add(item.content.call_id)
        decision = decisions_by_item.get(item.item_id)
        if decision is None:
            if decisions is None:
                decision, view = _new_decision(
                    item, call, policy, legacy=item.item_id in legacy_ids
                )
            else:
                decision = supplied.pop(item.item_id, None)
                if decision is None or decision.limit_utf8_bytes != policy.max_inline_utf8_bytes:
                    raise KernelError(
                        "context_tool_result_decision_mismatch", "缺少新结果决定或预算错绑"
                    )
                if item.item_id in legacy_ids and decision.strategy != "inline":
                    raise KernelError("context_tool_result_decision_mismatch", "旧历史不可重新裁剪")
                view = _apply_decision(item, call, decision, policy)
            created.append(decision)
        else:
            view = _apply_decision(item, call, decision, policy)
        used.append(decision)
        prepared.append(item.model_copy(deep=True, update={"content": view}))
    if supplied or (
        require_all_prior_decisions
        and not set(decisions_by_item).issubset({i.item_id for i in source_history})
    ):
        raise KernelError("context_tool_result_decision_mismatch", "模型视图决定引用未知Item")

    prepared_history = tuple(prepared)
    source_result_bytes = sum(decision.source_utf8_bytes for decision in used)
    view_result_bytes = sum(decision.view_utf8_bytes for decision in used)
    references = tuple(
        ModelHistoryArtifactReference(
            decision.call_id,
            binding,
            _omitted_field(decision) if binding.purpose == "tool_result" else None,
            _artifact_owner(thread, binding.artifact.artifact_id),
        )
        for decision in used
        for binding in decision.references
    )
    inspection = ModelHistoryInspection(
        model_step=model_step,
        policy=policy,
        history_items=len(source_history),
        tool_results=len(used),
        inline_results=sum(decision.strategy == "inline" for decision in used),
        artifact_reference_results=sum(
            decision.strategy == "artifact_reference" for decision in used
        ),
        artifact_bindings=len(references),
        source_tool_result_utf8_bytes=source_result_bytes,
        view_tool_result_utf8_bytes=view_result_bytes,
        source_history_sha256=_digest([item.model_dump(mode="json") for item in source_history]),
        view_history_sha256=_digest([item.model_dump(mode="json") for item in prepared_history]),
        decisions_sha256=_digest([decision.model_dump(mode="json") for decision in used]),
    )
    return PreparedModelHistory(
        history=prepared_history,
        inspection=inspection,
        new_decisions=tuple(created),
        references=references,
    )


def _omitted_field(decision: ToolResultViewDecision) -> ArtifactOmittedField | None:
    if not isinstance(decision.replacement_output, dict):
        return None
    omission = decision.replacement_output["model_view"]
    assert isinstance(omission, dict)
    return cast(ArtifactOmittedField, omission["omitted_field"])


def _artifact_owner(thread: Thread, artifact_id: UUID) -> UUID | None:
    if thread.fork_snapshot is None:
        return None
    return next(
        (
            owner.owner_thread_id
            for owner in thread.fork_snapshot.artifact_owners
            if owner.artifact_id == artifact_id
        ),
        None,
    )
