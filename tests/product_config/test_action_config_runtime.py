from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_codec import (
    canonical_product_action_config_bytes,
    decode_product_action_config_bytes,
    load_product_action_config,
    product_action_config_snapshot,
)
from harnessix.product_config.action_contracts import (
    ProductActionConfigSnapshot,
    build_product_action_config,
)
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, ProductConfigV2
from tests.product_config.conftest import write_config


def _write_action(path: Path, *, enabled: bool = True) -> Path:
    path.write_bytes(
        canonical_product_action_config_bytes(
            build_product_action_config(workspace_patch_enabled=enabled)
        )
    )
    if os.name == "posix":
        path.chmod(0o600)
    return path


def test_action_config_secure_load_builtin_and_strict_attacks(tmp_path: Path) -> None:
    builtin = load_product_action_config(None)
    path = _write_action(tmp_path / "actions.json")
    loaded = load_product_action_config(path)

    assert builtin.source_kind == "builtin"
    assert loaded.source_kind == "file"
    assert loaded.config == builtin.config
    assert loaded.source_sha256 != "0" * 64

    valid = json.loads(path.read_text(encoding="utf-8"))
    valid["unexpected"] = True
    with pytest.raises(KernelError) as unknown:
        decode_product_action_config_bytes(json.dumps(valid).encode())
    assert unknown.value.code == "product_action_config_invalid"

    duplicate = (
        '{"spec_version":"harnessix.product-action-config/v1",'
        '"workspace_patch_enabled":true,"workspace_patch_enabled":false,'
        '"process_profiles":[],"config_sha256":"' + "0" * 64 + '"}'
    ).encode()
    with pytest.raises(KernelError) as repeated:
        decode_product_action_config_bytes(duplicate)
    assert repeated.value.code == "product_action_config_invalid"

    with pytest.raises(KernelError) as size:
        decode_product_action_config_bytes(b"")
    assert size.value.code == "product_action_config_size"


def test_action_config_rejects_insecure_or_linked_file(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX配置文件权限与硬链接语义")
    path = _write_action(tmp_path / "actions.json")
    path.chmod(0o644)
    with pytest.raises(KernelError) as shared:
        load_product_action_config(path)
    assert shared.value.code == "product_action_config_permissions"

    path.chmod(0o600)
    linked = tmp_path / "linked.json"
    os.link(path, linked)
    with pytest.raises(KernelError) as hardlink:
        load_product_action_config(path)
    assert hardlink.value.code == "product_action_config_permissions"


def test_action_snapshot_digest_rejects_tampering() -> None:
    snapshot = product_action_config_snapshot(build_product_action_config())
    with pytest.raises(ValidationError):
        ProductActionConfigSnapshot.model_validate(
            {**snapshot.model_dump(), "config_sha256": "0" * 64}
        )


def test_product_and_action_activation_is_atomic_and_hash_chained(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    loaded = load_product_config(write_config(tmp_path / "config.json", config))
    assert isinstance(loaded, ProductConfigSnapshot)
    first = product_action_config_snapshot(build_product_action_config())
    second = product_action_config_snapshot(
        build_product_action_config(workspace_patch_enabled=False)
    )
    with SQLiteProductRuntimeConfigStore(tmp_path / "state" / "product-config.db") as store:
        product_event, action_event = store.activate_runtime(
            loaded,
            "primary",
            first,
            expected_active_sha256=None,
            expected_active_action_sha256=None,
        )
        assert product_event is not None and product_event.operation == "activated"
        assert action_event is not None and action_event.operation == "activated"
        assert store.active() == (loaded.config_sha256, "primary")
        assert store.active_action() == first.config_sha256
        assert [item.operation for item in store.action_config_events()] == [
            "loaded",
            "activated",
        ]

        assert store.activate_runtime(
            loaded,
            "primary",
            first,
            expected_active_sha256=None,
            expected_active_action_sha256=None,
        ) == (None, None)

        with pytest.raises(KernelError) as action_conflict:
            store.activate_runtime(
                loaded,
                "backup",
                second,
                expected_active_sha256=loaded.config_sha256,
                expected_active_profile="primary",
                expected_active_action_sha256="0" * 64,
            )
        assert action_conflict.value.code == "product_action_config_conflict"
        assert store.active() == (loaded.config_sha256, "primary")
        assert store.active_action() == first.config_sha256

        with pytest.raises(KernelError) as conflict:
            store.activate_runtime(
                loaded,
                "backup",
                second,
                expected_active_sha256="0" * 64,
                expected_active_profile="primary",
                expected_active_action_sha256=first.config_sha256,
            )
        assert conflict.value.code == "product_config_conflict"
        assert store.active() == (loaded.config_sha256, "primary")
        assert store.active_action() == first.config_sha256

        changed_product, changed_action = store.activate_runtime(
            loaded,
            "backup",
            second,
            expected_active_sha256=loaded.config_sha256,
            expected_active_profile="primary",
            expected_active_action_sha256=first.config_sha256,
        )
        assert changed_product is not None and changed_action is not None
        assert store.active() == (loaded.config_sha256, "backup")
        assert store.active_action() == second.config_sha256

        store._db.execute(  # noqa: SLF001 - 故障注入验证Hash链失败关闭
            "UPDATE product_action_config_events SET digest = ? WHERE sequence = 1",
            ("0" * 64,),
        )
        with pytest.raises(KernelError) as corrupt:
            store.action_config_events()
        assert corrupt.value.code == "product_action_config_store_corrupt"
