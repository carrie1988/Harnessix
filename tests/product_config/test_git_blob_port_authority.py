"""原产品控制读端口必须保持原 Store 绑定；端口不是可替换的认证证明。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_user_authority import require_git_user_authority
from tests.support.git_user_observation import run_authenticated_observation


@pytest.mark.parametrize("replacement", ["none", "wrapped", "foreign-store"])
async def test_original_controlled_reader_rebinding_is_rejected_without_reads_or_writes(
    tmp_path, config, monkeypatch, replacement
):
    async def inspect(scenario):
        ports = scenario.router._snapshot_ports
        verify = require_git_user_authority(
            scenario.session, scenario.router, scenario.transactions, ports, scenario.reader
        )
        original = ports.controlled_read_blob
        assert original.__self__ is scenario.transactions
        before = scenario.unchanged_state()
        with SQLiteWorkspaceTransactionStore(tmp_path / "foreign-transactions") as other:
            changed = {
                "none": None,
                "wrapped": lambda digest, mark: original(digest, mark),
                "foreign-store": other.controlled_blob,
            }[replacement]
            object.__setattr__(ports, "controlled_read_blob", changed)
            try:
                with pytest.raises(KernelError) as caught:
                    verify()
                assert caught.value.code == "git_user_observation_host_invalid"
            finally:
                object.__setattr__(ports, "controlled_read_blob", original)
        verify()
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)


async def test_forged_bound_method_metadata_is_not_an_original_controlled_port(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        ports = scenario.router._snapshot_ports
        original, before = ports.controlled_read_blob, scenario.unchanged_state()

        class ForgedMethod:
            def __init__(self):
                self.__self__ = scenario.transactions
                self.__func__ = original.__func__

            def __call__(self, *_args):
                pytest.fail("forged port must be rejected before dispatch")

        forged = ForgedMethod()
        assert forged.__self__ is scenario.transactions
        assert forged.__func__ is original.__func__
        object.__setattr__(ports, "controlled_read_blob", forged)
        try:
            with pytest.raises(KernelError) as caught:
                require_git_user_authority(
                    scenario.session, scenario.router, scenario.transactions, ports, scenario.reader
                )
            assert caught.value.code == "git_user_observation_host_invalid"
        finally:
            object.__setattr__(ports, "controlled_read_blob", original)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)
