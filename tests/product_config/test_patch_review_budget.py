"""审批预览准备的读取期限：慢机器上的大补丁不因模型读工具的5秒上限失败，超时也不变成未知效果。"""

from __future__ import annotations

import io

from harnessix.delivery import action_transaction_planning
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import PublicApprovalRequestContent, PublicToolResultContent
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS
from harnessix.tools.workspace import ReadOperation
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step
from tests.product_config.conftest import write_config
from tests.product_config.test_parent_closure_product_sdk import _proposal
from tests.product_config.test_product_rollback_sdk import ProductBundle, contents, wait_turn
from tests.product_config.test_server_and_cli import _credentials


def _planning_started_late(elapsed_seconds: float) -> type[ReadOperation]:
    """模拟规划开始时已耗去elapsed_seconds（如繁忙磁盘上观察深层父目录）。"""

    class Late(ReadOperation):
        def __init__(self, *, timeout_seconds: float) -> None:
            super().__init__(timeout_seconds=timeout_seconds)
            self.deadline -= elapsed_seconds

    return Late


async def _patch_in_default_product(tmp_path, config, monkeypatch, until):
    """经默认产品与SDK提交一个父目录较深的补丁，返回到达until状态时的回合与公开内容。"""

    _credentials(monkeypatch)
    root, state = tmp_path / "workspace", tmp_path / "state"
    proposal = _proposal(root, count=2, depth=4)
    bundle, observed = ProductBundle([]), {}

    async def build(*_args, **_kwargs):
        return bundle

    async def drive(server, *_streams):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(root), request_id="budget-thread")
            bundle.steps = (_action_step(proposal), answer("已处理"))
            await client.start_turn(thread.thread_id, "修改文件", request_id="budget-patch")
            turn = await wait_turn(client, thread.thread_id, until)
            for model in (PublicApprovalRequestContent, PublicToolResultContent):
                observed[model] = await contents(client, thread.thread_id, turn.turn_id, model)
        finally:
            await client.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    await run_product_stdio(
        config_path=write_config(tmp_path / "config.json", config),
        profile_id=None,
        workspace=root,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    assert all((root / item.path).read_bytes() == b"before\n" for item in proposal.files)
    return observed


async def test_slow_review_preparation_beyond_model_read_budget_still_reaches_approval(
    tmp_path, config, monkeypatch
):
    monkeypatch.setattr(
        action_transaction_planning,
        "ReadOperation",
        _planning_started_late(READ_TIMEOUT_SECONDS + 1),
    )
    observed = await _patch_in_default_product(tmp_path, config, monkeypatch, "waiting_approval")

    assert len(observed[PublicApprovalRequestContent]) == 1
    assert observed[PublicToolResultContent] == []


async def test_review_preparation_timeout_is_recoverable_feedback_not_unknown_effect(
    tmp_path, config, monkeypatch
):
    monkeypatch.setattr(
        action_transaction_planning,
        "ReadOperation",
        _planning_started_late(action_transaction_planning.ACTION_PLANNING_READ_TIMEOUT_SECONDS),
    )
    observed = await _patch_in_default_product(tmp_path, config, monkeypatch, "completed")

    assert observed[PublicApprovalRequestContent] == []
    # 公开回放同时含item_started与item_finished，二者内容相同。
    results = observed[PublicToolResultContent]
    assert {item.call_id for item in results} == {results[-1].call_id}
    result = results[-1]
    assert result.outcome == "failed"
    assert result.error.code == "workspace_patch_preparation_timeout"
    assert "本次Patch未执行，工作区没有变化" in result.error.message
