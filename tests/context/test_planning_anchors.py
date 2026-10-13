"""计划保留原始配对工具组，不把模型自报提升为验收事实。"""

from uuid import uuid4, uuid5

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import PlanContent, PlanStep, ToolCallContent, ToolResultContent
from harnessix.agent.planning import planning_history_anchors, planning_tool_descriptor
from harnessix.context.compaction import validate_compaction
from tests.context.test_compaction import (
    anchor,
    assert_paired_partition,
    item,
    plan,
    summary,
    text,
    thread_with,
    tool_group,
)


def native_plan_group(*, description="检查证据后修改，再运行验证", supersedes=None):
    descriptor = planning_tool_descriptor()
    call_id = uuid4()
    steps = (PlanStep(step_id="inspect", description=description, status="in_progress"),)
    call = item(
        ToolCallContent(
            call_id=call_id,
            provider_call_id="plan",
            tool=descriptor.name,
            tool_version=descriptor.version,
            tool_fingerprint=tool_fingerprint(descriptor),
            effect_class=descriptor.effect_class,
            arguments={"steps": [step.model_dump(mode="json") for step in steps]},
        )
    )
    plan_id = uuid5(call_id, "harnessix.plan-item/v1")
    result = item(
        ToolResultContent(
            call_id=call_id,
            outcome="succeeded",
            output={"plan_item_id": str(plan_id), "step_count": 1, "self_reported": True},
        )
    )
    native = item(PlanContent(steps=steps, supersedes=supersedes)).model_copy(
        update={"item_id": plan_id}
    )
    return call, result, native


async def test_exact_source_plan_keeps_entire_closed_group_with_other_reads():
    call, result, native = native_plan_group()
    read_message, read_call, read_result = tool_group(count=1, output="fact" * 10)
    group = (read_message, read_call, call, read_result, result)
    source = thread_with(
        (text("任务", "user_message"), *group, native, *tool_group(), *tool_group(), text("继续"))
    )
    pins = planning_history_anchors(source)
    assert pins == (anchor(call),)
    prepared = await plan(source, anchors=pins, target_history_tokens=6000)
    assert {entry.item_id for entry in group} <= set(prepared.plan.pinned_item_ids)
    assert native.item_id not in prepared.plan.retained_item_ids
    assert_paired_partition(prepared)
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    assert {entry.item_id for entry in group} <= {entry.item_id for entry in candidate.history}
    assert source.turns[0].items[6] == native


@pytest.mark.parametrize("tamper", ["steps", "version", "fingerprint", "result", "identity"])
def test_pin_requires_exact_native_steps_identity_and_paired_acknowledgement(tamper):
    call, result, native = native_plan_group()
    if tamper == "steps":
        native = native.model_copy(
            update={"content": PlanContent(steps=(PlanStep(step_id="other", description="伪报"),))}
        )
    elif tamper in {"version", "fingerprint"}:
        field = "tool_version" if tamper == "version" else "tool_fingerprint"
        call = call.model_copy(update={"content": call.content.model_copy(update={field: "x"})})
    elif tamper == "result":
        output = {**result.content.output, "self_reported": False}
        result = result.model_copy(
            update={"content": result.content.model_copy(update={"output": output})}
        )
    else:
        native = native.model_copy(update={"item_id": uuid4()})
    assert planning_history_anchors(thread_with((call, result, native))) == ()


async def test_latest_revision_only_is_pinned_and_manual_old_plan_does_not_change_compaction():
    old = native_plan_group()
    latest = native_plan_group(description="基于新事实修改计划", supersedes=old[-1].item_id)
    source = thread_with((text("任务", "user_message"), *old, *tool_group(), *latest, text("继续")))
    assert planning_history_anchors(source) == (anchor(latest[0]),)
    manual = item(PlanContent(steps=(PlanStep(step_id="manual", description="旧客户端计划"),)))
    legacy = thread_with((text("任务", "user_message"), *tool_group(), manual, text("继续")))
    assert planning_history_anchors(legacy) == ()
    identity = uuid4()
    assert (await plan(legacy, identity=identity)).plan == (
        await plan(legacy, identity=identity, anchors=())
    ).plan


async def test_oversized_plan_pin_cannot_relax_history_budget():
    native = native_plan_group(description="计划" * 500)
    source = thread_with((text("任务", "user_message"), *native, *tool_group(), text("继续")))
    with pytest.raises(KernelError) as error:
        await plan(source, anchors=planning_history_anchors(source), target_history_tokens=2500)
    assert error.value.code == "context_compaction_retained_overflow"
