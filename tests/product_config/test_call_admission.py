"""调用准入：视图拒绝的Patch以可纠正失败结束，不请求审批、不准备计划、不改动文件。"""

from __future__ import annotations

from harnessix.agent.errors import AgentFailure, FailureCategory
from harnessix.agent.models import TrustedActionApprovalRequestContent, TurnStatus
from harnessix.agent.ports import ModelToolSelection
from tests.delivery.test_trusted_action_patch import _proposal
from tests.product_config.test_patch_precondition_rejection import assert_unchanged, patch_turn
from tests.product_config.test_product_patch_rollback import product, result

FAILURE = AgentFailure(
    code="workspace_patch_reproduction_required",
    message="先复现",
    category=FailureCategory.TOOL,
    retryable=False,
)


class AdmissionView:
    def __init__(self, failure):
        self.failure = failure
        self.seen = []

    def select(self, turn, definitions):
        return ModelToolSelection(definitions)

    def admit_call(self, turn, call):
        self.seen.append(call.tool)
        return self.failure


async def test_rejected_call_is_a_failed_result_before_approval_or_effect(tmp_path):
    view = AdmissionView(FAILURE)
    async with product(tmp_path, model_tool_view=view) as (root, runtime, provider, *_):
        _thread, turn, call = await patch_turn(runtime, provider, root, _proposal())
        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED
        assert view.seen == [call.tool]
        assert output.outcome == "failed" and output.error == FAILURE
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )
        assert_unchanged(root)


async def test_admitted_call_keeps_the_original_approval_path(tmp_path):
    view = AdmissionView(None)
    async with product(tmp_path, model_tool_view=view) as (root, runtime, provider, *_):
        _thread, turn, call = await patch_turn(runtime, provider, root, _proposal())
        assert view.seen == [call.tool]
        assert turn.status is TurnStatus.WAITING_APPROVAL
        assert any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )


async def test_invalid_admission_result_fails_closed(tmp_path):
    view = AdmissionView("not a failure")
    async with product(tmp_path, model_tool_view=view) as (root, runtime, provider, *_):
        _thread, turn, _call = await patch_turn(runtime, provider, root, _proposal())
        # 写入类工具阶段的任何宿主异常按既有规则中断为未知效果（不执行、不自动重放），而非静默放行。
        assert turn.status is TurnStatus.INTERRUPTED
        assert turn.error is not None and turn.error.code == "uncertain_effect"
        assert_unchanged(root)
