"""实际SDK成功Patch来源的只读复核；不以声明级摘要替代原认证历史。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_source import verify_git_delivery_source
from tests.support.git_user_observation import run_authenticated_observation


@pytest.mark.parametrize("continuous", [False, True])
async def test_original_authenticated_source_can_be_verified_without_capture_or_cas_write(
    tmp_path, config, monkeypatch, continuous
):
    async def inspect(scenario):
        observation = await scenario.collect()
        before = scenario.unchanged_state()
        source = observation.baseline.source
        verify_git_delivery_source(
            scenario.thread,
            source,
            scenario.router,
            scenario.transactions,
            checkpoint=CancelToken().checkpoint,
            snapshot_ports=scenario.router._snapshot_ports,
        )
        assert scenario.unchanged_state() == before
        assert source == observation.baseline.source

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, continuous=continuous
    )


@pytest.mark.parametrize("change", ["body", "mode", "missing"])
async def test_actual_final_source_body_mode_or_presence_change_fails_closed(
    tmp_path, config, monkeypatch, change
):
    async def inspect(scenario):
        observation = await scenario.collect()
        source = observation.baseline.source
        selected = scenario.root / scenario.selected_paths[0]
        assert hashlib.sha256(selected.read_bytes()).hexdigest() == source.mutations[0].after.sha256
        if change == "body":
            selected.write_bytes(b"later independent change\n")
        elif change == "mode":
            selected.chmod(0o755)
        else:
            selected.unlink()
        before = scenario.unchanged_state()
        with pytest.raises(KernelError):
            verify_git_delivery_source(
                scenario.thread,
                source,
                scenario.router,
                scenario.transactions,
                checkpoint=CancelToken().checkpoint,
                snapshot_ports=scenario.router._snapshot_ports,
            )
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_actual_net_zero_path_still_requires_current_final_source_version(
    tmp_path, config, monkeypatch
):
    """净Mutation不包含净零路径，但完整Patch链的最终版本仍必须验真。"""
    from tests.support import git_user_observation as support

    original = support._action_step

    def action_step(proposal):
        if proposal.files[0].content == "final\n":
            proposal = proposal.model_copy(
                update={
                    "files": (
                        proposal.files[0].model_copy(update={"content": "before\n"}),
                        *proposal.files[1:],
                    )
                }
            )
        return original(proposal)

    monkeypatch.setattr(support, "_action_step", action_step)

    async def inspect(scenario):
        source = (await scenario.collect()).baseline.source
        net_zero = scenario.selected_paths[0]
        assert net_zero not in {item.path for item in source.mutations}
        assert net_zero in {item.path for item in source.workspace.resources}
        before = scenario.unchanged_state()
        verify_git_delivery_source(
            scenario.thread,
            source,
            scenario.router,
            scenario.transactions,
            checkpoint=CancelToken().checkpoint,
            snapshot_ports=scenario.router._snapshot_ports,
        )
        (scenario.root / net_zero).write_bytes(b"independent later modification\n")
        with pytest.raises(KernelError):
            verify_git_delivery_source(
                scenario.thread,
                source,
                scenario.router,
                scenario.transactions,
                checkpoint=CancelToken().checkpoint,
                snapshot_ports=scenario.router._snapshot_ports,
            )
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)
