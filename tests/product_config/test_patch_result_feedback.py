"""产品Patch成功结果经真实Gateway公开v2：计划路径绑定、发布后摘要可直接用于下一次Patch。"""

from __future__ import annotations

import hashlib
import json

from harnessix.agent.models import TurnStatus
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.execution.public_tool_contracts import PublicWorkspacePatchOutputV2
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _proposal
from tests.product_config.test_patch_precondition_rejection import patch_turn
from tests.product_config.test_product_patch_rollback import approve, product, result


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def test_product_result_is_v2_and_its_digest_is_the_next_replace_precondition(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        provider.steps = (_action_step(_proposal()), answer("修改完成"))
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改三个文件", request_id="patch")
        completed = await approve(runtime, thread.thread_id, waiting)
        first = result(completed)
        assert completed.status is TurnStatus.COMPLETED and first.outcome == "succeeded"
        output = PublicWorkspacePatchOutputV2.model_validate_json(
            json.dumps(first.output, ensure_ascii=False)
        )
        assert output.spec_version == "harnessix.workspace-patch-output/v2"
        assert output.transaction_id == first.trusted_action.plan_id and output.files == 3
        digests = {change.path: change.content_sha256 for change in output.changes}
        assert digests == {
            "src/modified.py": sha((root / "src/modified.py").read_bytes()),
            "src/新增.py": sha((root / "src/新增.py").read_bytes()),
            "tests/deleted.txt": None,
        }
        assert digests["src/modified.py"] == sha(b"new\n")
        assert not output.diff_preview.truncated
        assert sha(output.diff_preview.text.encode()) == output.diff_sha256
        assert "+new" in output.diff_preview.text

        # replace不由宿主补SHA：只有模型提交的摘要正确才能通过规划与执行。
        follow_up = WorkspacePatchInput(
            files=(
                WorkspacePatchFile(
                    operation="replace",
                    path="src/modified.py",
                    expected_sha256=digests["src/modified.py"],
                    content="newer\n",
                    mode=0o644,
                ),
            )
        )
        thread, waiting, call = await patch_turn(runtime, provider, root, follow_up)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert call.arguments["files"][0]["expected_sha256"] == digests["src/modified.py"]
        completed = await approve(runtime, thread.thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        assert (root / "src/modified.py").read_bytes() == b"newer\n"
        second = PublicWorkspacePatchOutputV2.model_validate_json(
            json.dumps(result(completed).output, ensure_ascii=False)
        )
        assert [(change.path, change.content_sha256) for change in second.changes] == [
            ("src/modified.py", sha(b"newer\n"))
        ]
