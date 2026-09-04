from uuid import uuid4

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.models import ToolCallContent
from harnessix.patches.batch_contracts import PatchBatchProposal
from tests.patches.bridge_helpers import make_scope


def make_call(copy, bridge, prepared):
    proposal = PatchBatchProposal(files=tuple(p.proposal for p in prepared.patches))
    definition = bridge.definition()
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="batch-call",
        tool=definition.name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        requires_approval=definition.requires_approval,
        tool_fingerprint=tool_fingerprint(definition),
        arguments=proposal.model_dump(mode="json"),
    )
    return call, make_scope(copy, call)
