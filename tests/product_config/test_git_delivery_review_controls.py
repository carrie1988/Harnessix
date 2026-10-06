"""真实认证Git Review的末端取消、Owner、TTL、故障和跨块保护回归。"""

from __future__ import annotations

import hashlib
import inspect

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.diff_content import build_diff_content
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.product_config import git_delivery_review as module
from harnessix.product_config.git_delivery_review_codec import decode_product_git_action_review
from tests.support import git_user_observation as observation_support
from tests.support.git_delivery_review import pending_actual_review


@pytest.mark.parametrize("change", ["cancel", "owner", "expire", "provider_reference"])
async def test_actual_review_terminal_settlement_gap_is_fail_closed(
    tmp_path, config, monkeypatch, change
):
    """仅在原CancelToken.run实际结算完后注入末端变化，不能被子任务检查遮蔽。"""

    async def scenario_check(scenario):
        original_run = CancelToken.run
        saved = {}

        async def observed_run(token, operation):
            producing = inspect.iscoroutine(operation) and operation.cr_code.co_name == "_produce"
            result = await original_run(token, operation)
            if producing:
                saved["reference"] = result.diff_artifact
                if change == "cancel":
                    token.cancel()
                elif change == "owner":
                    monkeypatch.setattr(scenario.session, "_runtime_owner_token", object())
                elif change == "expire":
                    monkeypatch.setattr(module, "utc_now", lambda: result.diff_artifact.expires_at)
                else:
                    saved["provider"]._reader = object()
            return result

        original_review = module.ProductGitReviewProvider.review

        async def observed_review(provider, *args):
            saved["provider"] = provider
            return await original_review(provider, *args)

        async def unreachable(_actual):
            pytest.fail("末端变化不得产生可用审批")

        with monkeypatch.context() as context:
            # 必须在检查期间恢复Owner，避免测试故障干扰真实宿主正常退出。
            owner = scenario.session._runtime_owner_token
            context.setattr(CancelToken, "run", observed_run)
            context.setattr(module.ProductGitReviewProvider, "review", observed_review)
            try:
                expected = TurnCancelled if change == "cancel" else KernelError
                with pytest.raises(expected) as caught:
                    await pending_actual_review(
                        scenario, monkeypatch, unreachable, with_provider=True
                    )
                if isinstance(caught.value, KernelError):
                    assert caught.value.code == (
                        "artifact_expired"
                        if change == "expire"
                        else "git_action_review_host_invalid"
                    )
                assert saved["reference"] is not None
            finally:
                scenario.session._runtime_owner_token = owner

    await observation_support.run_authenticated_observation(
        tmp_path, config, monkeypatch, scenario_check
    )


@pytest.mark.parametrize("stage", ["action_review.after_insert", "action_review.after_commit"])
async def test_actual_review_original_publish_fault_and_same_receipt_replay(
    tmp_path, config, monkeypatch, stage
):
    """原事务回滚或查询优先应答恢复，既有身份/正文/TTL不能重新生成。"""

    async def scenario_check(scenario):
        original_review = module.ProductGitReviewProvider.review
        calls, saved = [], {}

        def fault(point):
            if point == stage:
                calls.append(point)
                raise KernelError("test_review_fault", "正式故障注入")

        async def observed_review(provider, *args):
            saved["call"] = args[3]
            monkeypatch.setattr(provider._artifacts, "_fault", fault)
            return await original_review(provider, *args)

        async def approved_reference(actual):
            approval = next(
                item.content
                for item in actual.turn.items
                if getattr(item.content, "kind", None) == "trusted_action_approval_request"
            )
            ref = approval.diff_artifact
            page = await scenario.client.read_artifact(actual.thread.thread_id, ref.artifact_id)
            document = decode_product_git_action_review(page.text.encode(), checkpoint=lambda: None)
            assert document.summary.call == actual.call
            replayed = await actual.artifacts.publish_action_review(
                actual.thread.thread_id,
                actual.turn.turn_id,
                actual.call,
                page.text.encode(),
                artifact_id=ref.artifact_id,
                workspace_scope=actual.provider._workspace_scope,
                expected_sequence=actual.thread.sequence,
            )
            assert replayed == ref  # 原过期时刻、身份、正文和记录数全部相同。

        with monkeypatch.context() as context:
            context.setattr(module.ProductGitReviewProvider, "review", observed_review)
            if stage.endswith("after_insert"):
                with pytest.raises(KernelError) as caught:
                    await pending_actual_review(
                        scenario, monkeypatch, approved_reference, with_provider=True
                    )
                assert caught.value.code == "test_review_fault"
                async with scenario.session._connection() as database:
                    cursor = await database.execute(
                        "SELECT COUNT(*) FROM agent_artifacts WHERE call_id=?",
                        (str(saved["call"].call_id),),
                    )
                    assert (await cursor.fetchone())[0] == 0
            else:
                await pending_actual_review(
                    scenario, monkeypatch, approved_reference, with_provider=True
                )
            assert calls == [stage]

    await observation_support.run_authenticated_observation(
        tmp_path, config, monkeypatch, scenario_check
    )


async def test_actual_reopened_scope_rejects_prior_review_spanning_chunks(
    tmp_path, config, monkeypatch
):
    """原持久MAC与回指仍成立，但新Scope必须在SDK读侧拒绝已知完整原文。"""
    from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation

    synthetic = "review-boundary-synthetic-value"
    original_proposal = observation_support._proposal

    def proposal(root, **kwargs):
        original = original_proposal(root, **kwargs)
        first = original.files[0]
        before, after = b"before\n", b"after\n"
        mutation = WorkspaceMutation(
            path=first.path,
            before=WorkspaceFileVersion(
                presence="file",
                sha256=hashlib.sha256(before).hexdigest(),
                size=len(before),
                mode=420,
            ),
            after=WorkspaceFileVersion(
                presence="file", sha256=hashlib.sha256(after).hexdigest(), size=len(after), mode=420
            ),
        )
        sample = build_diff_content(
            (mutation,),
            lambda sha: before if sha == mutation.before.sha256 else after,
            max_utf8_bytes=1024 * 1024,
            checkpoint=lambda: None,
        )
        prefix = sample.text.index("-before\n") + 1
        body = ("x" * (57000 - 8 - prefix) + synthetic + "\n").encode()
        (root / first.path).write_bytes(body)
        return WorkspacePatchInput(
            files=(
                first.model_copy(update={"expected_sha256": hashlib.sha256(body).hexdigest()}),
                *original.files[1:],
            )
        )

    monkeypatch.setattr(observation_support, "_proposal", proposal)
    protected = []
    original_protect = module.protect_json

    async def inspect_scenario(scenario):
        if not scenario.reopened:
            # 仅合成测试凭据；不访问宿主Keychain、launchctl或真实Provider。
            monkeypatch.setenv("PRIMARY_API_KEY", synthetic)
            return

        from harnessix.agent.models import TrustedActionApprovalRequestContent
        from harnessix.sdk import AgentSDKError

        previous = next(
            item.content
            for turn in scenario.thread.turns
            for item in turn.items
            if type(item.content) is TrustedActionApprovalRequestContent
        )
        with pytest.raises(AgentSDKError) as denied:
            await scenario.client.read_artifact(
                scenario.thread.thread_id, previous.diff_artifact.artifact_id
            )
        assert denied.value.code == "public_output_secret_leak"

        async def observed_protect(scope, value, cancel):
            assert scope is scenario.session._publication._events._protection
            assert synthetic in value
            assert all(synthetic not in value[i : i + 1900] for i in range(0, len(value), 1900))
            assert all(synthetic not in value[i : i + 3000] for i in range(0, len(value), 3000))
            protected.append(True)
            return await original_protect(scope, value, cancel)

        async def unreachable(_actual):
            pytest.fail("跨块受保护材料不得交付审批")

        with monkeypatch.context() as context:
            context.setattr(module, "protect_json", observed_protect)
            with pytest.raises(AssertionError, match="public_output_secret_leak"):
                await pending_actual_review(scenario, monkeypatch, unreachable, with_provider=True)
        # 原Kernel复核旧Artifact时已拒绝，不能等到新Git Review才恢复保护。
        assert protected == []

    await observation_support.run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect_scenario, reopen=True
    )


async def test_actual_review_expired_total_deadline_cannot_publish(tmp_path, config, monkeypatch):
    """仅压缩测试的单次期限，不改变产品默认60秒或重置子阶段预算。"""
    assert module._BASELINE_TIMEOUT_SECONDS == 60.0

    async def inspect_scenario(scenario):
        async def unreachable(_actual):
            pytest.fail("到期审阅不得生成可用审批")

        with monkeypatch.context() as context:
            context.setattr(module, "_BASELINE_TIMEOUT_SECONDS", 0.000001)
            with pytest.raises(KernelError) as caught:
                await pending_actual_review(scenario, monkeypatch, unreachable, with_provider=True)
            assert caught.value.code == "git_process_timeout"

    await observation_support.run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect_scenario
    )
