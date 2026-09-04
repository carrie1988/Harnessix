from uuid import uuid4

from harnessix.agent.approvals import execution_fingerprint, tool_fingerprint
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation


def make_scope(copy, call, *, thread_id=None, turn_id=None):
    thread_id, turn_id = thread_id or uuid4(), turn_id or uuid4()
    workspace = str(copy.workspace.root)
    return ToolExecutionScope(
        thread_id,
        turn_id,
        call.call_id,
        workspace,
        execution_fingerprint(thread_id, turn_id, workspace, call),
    )


def make_call(copy, bridge):
    page = read_file(copy.workspace, ReadFileInput(path="main.py"), ReadOperation())
    proposal = PatchProposal(
        path="main.py",
        expected_revision=page.revision,
        edits=(ExactEdit(old_text="before", new_text="after"),),
    )
    definition = bridge.definition()
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="model-call",
        tool=definition.name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        arguments=proposal.model_dump(mode="json"),
        requires_approval=definition.requires_approval,
        tool_fingerprint=tool_fingerprint(definition),
    )
    return call, make_scope(copy, call)


def approval(plan, outcome=ApprovalOutcome.APPROVED):
    return ApprovalRecord(
        request_fingerprint=plan.approval_fingerprint, outcome=outcome, actor="桥接验收宿主"
    )
