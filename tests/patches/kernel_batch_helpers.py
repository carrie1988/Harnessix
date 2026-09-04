from harnessix.agent.models import PatchBatchApprovalRequestContent
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.test_kernel_patch import APPROVE


def batch_step(copy, bridge, prepared, *, arguments=None):
    call, _ = make_call(copy, bridge, prepared)
    return [
        ResponseStarted(response_id="batch"),
        ToolCallCompleted(
            call_id="batch-1",
            tool="apply_patch_batch",
            arguments=call.arguments if arguments is None else arguments,
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def approval_of(turn):
    return next(
        i.content for i in turn.items if isinstance(i.content, PatchBatchApprovalRequestContent)
    )


async def decide(runtime, thread_id, turn, decision=APPROVE):
    request = approval_of(turn)
    return await runtime.reply_approval(
        thread_id,
        turn.turn_id,
        request.approval_id,
        fingerprint=request.request_fingerprint,
        decision=decision,
    )
