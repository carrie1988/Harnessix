"""改过配置的工作区仍能打开：产品入口把当前活动指针作为切换前提交给Agent Server（0.10.0b8）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, ProductConfigV2
from harnessix.product_ui.activation import activation_arguments
from tests.product_config.conftest import write_config


def _snapshot(path: Path, config: ProductConfigV2) -> ProductConfigSnapshot:
    snapshot = load_product_config(write_config(path, config))
    assert isinstance(snapshot, ProductConfigSnapshot)
    return snapshot


def test_a_reconfigured_workspace_switches_instead_of_closing(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    state = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    before = _snapshot(tmp_path / "config.json", config)
    store = SQLiteProductRuntimeConfigStore(state / "product-config.db")
    store.activate(before, "primary", expected_active_sha256=None)
    store.close()
    # 用户用configure换了模型：配置文件变了，工作区的活动指针仍是旧配置。
    profiles = (
        config.profiles[0].model_copy(update={"model": "another-model"}),
        *config.profiles[1:],
    )
    after = _snapshot(tmp_path / "config.json", config.model_copy(update={"profiles": profiles}))
    assert after.config_sha256 != before.config_sha256

    arguments = activation_arguments(state, (None, None, None))
    assert arguments == (
        "--expected-active-sha256",
        before.config_sha256,
        "--expected-active-profile",
        "primary",
    )

    store = SQLiteProductRuntimeConfigStore(state / "product-config.db")
    try:
        # 此前入口不给前提，Server在这里以product_config_conflict退出，界面只显示server_closed。
        with pytest.raises(KernelError) as conflict:
            store.activate(after, "primary", expected_active_sha256=None)
        assert conflict.value.code == "product_config_conflict"
        store.activate(
            after,
            "primary",
            expected_active_sha256=arguments[1],
            expected_active_profile=arguments[3],
        )
        assert store.active() == (after.config_sha256, "primary")
    finally:
        store.close()


def test_explicit_preconditions_and_fresh_workspaces_are_left_alone(tmp_path: Path) -> None:
    assert activation_arguments(tmp_path, (None, None, None)) == ()
    explicit = ("a" * 64, "primary", None)
    assert activation_arguments(tmp_path, explicit) == (
        "--expected-active-sha256",
        "a" * 64,
        "--expected-active-profile",
        "primary",
    )
