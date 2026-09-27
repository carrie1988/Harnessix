"""独立旧版本创建实际SQLite计划和审批，再由新版本验真重开，不重签历史。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.domain.models import ToolDescriptor
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway

OLD_REVISION = "f7566bf0e594833bd10a1af7d9d8b51a098cfad9"
PROGRAM = """
import asyncio,json,sys
from pathlib import Path
import harnessix.trusted_actions.agent_gateway_output as implementation
assert Path(implementation.__file__).is_relative_to(Path.cwd())
from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.domain.models import ApprovalDecision,ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_agent_gateway import (
 FakeExecutor,descriptor,build_gateway,agent_state,
)
async def main():
 root=Path(sys.argv[1]);root.mkdir(parents=True);(root/'file.txt').write_text('unchanged')
 executor=FakeExecutor(ActionExecutionOutcome(kind='succeeded',output={'summary':'legacy'}))
 gate,actions,plans,audit=build_gateway(root,executor)
 thread,turn,call=agent_state(root)
 request=await gate.prepare(thread,turn,call,CancelToken())
 approved=gate.decide(thread,turn,call,request,ApprovalDecision(outcome=ApprovalOutcome.APPROVED,actor='old-binary'))
 await actions.execute(request.plan_id)
 frozen=actions.status(request.plan_id)
 record={
  'revision':sys.argv[2],'module':implementation.__file__,
  'descriptor':descriptor().model_dump_json(),
  'tool_fingerprint':tool_fingerprint(descriptor()),'plan':frozen.plan.model_dump_json(),
  'events':[e.model_dump_json() for e in actions.events(request.plan_id)],
  'thread':thread.model_dump_json(),'turn':turn.model_dump_json(),
  'call':call.model_dump_json(),'approval':approved.model_dump_json(),
  'execute_calls':executor.calls,
 }
 gate.close();plans.close();audit.close()
 (root.parent/'old-record.json').write_text(json.dumps(record),encoding='utf-8')
asyncio.run(main())
"""


@pytest.fixture
def old_record(tmp_path):
    import io
    import tarfile

    repo = Path(__file__).resolve().parents[2]
    archive = tmp_path / "old-binary"
    archive.mkdir()
    raw = subprocess.run(
        [
            "git",
            "archive",
            OLD_REVISION,
            "src",
            "tests/__init__.py",
            "tests/trusted_actions/__init__.py",
            "tests/trusted_actions/test_agent_gateway.py",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as source:
        source.extractall(archive, filter="data")
    root = tmp_path / "state" / "workspace"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(archive / "src"), str(archive))))
    subprocess.run(
        [sys.executable, "-c", PROGRAM, str(root), OLD_REVISION],
        cwd=archive,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    record = json.loads((root.parent / "old-record.json").read_text())
    assert Path(record["module"]).is_relative_to(archive)
    assert record["execute_calls"] == 1
    return root, record


async def test_real_old_binary_plan_and_approval_survive_new_descriptor_field(old_record):
    root, record = old_record
    tool = ToolDescriptor.model_validate_json(record["descriptor"])
    assert tool.public_output_schema is None
    assert tool.model_dump_json() == record["descriptor"]
    assert tool_fingerprint(tool) == record["tool_fingerprint"]
    second = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, actions, plans, audit = build_gateway(root, second, tool=tool)
    try:
        thread = Thread.model_validate_json(record["thread"])
        turn = Turn.model_validate_json(record["turn"])
        call = ToolCallContent.model_validate_json(record["call"])
        approved = TrustedActionApprovalRequestContent.model_validate_json(record["approval"])
        before = actions.status(approved.plan_id)
        assert before.plan.model_dump_json() == record["plan"]
        assert [e.model_dump_json() for e in actions.events(approved.plan_id)] == record["events"]
        result = await gateway.recover(thread, turn, call, approved, CancelToken())
        assert result.output is None and result.trusted_action.state == "succeeded"
        assert actions.status(approved.plan_id) == before
        assert [e.model_dump_json() for e in actions.events(approved.plan_id)] == record["events"]
        assert second.calls == second.reconciliations == 0
    finally:
        audit.close()
        plans.close()
