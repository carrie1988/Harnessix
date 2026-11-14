from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import decode_product_config_bytes, load_product_config
from harnessix.product_config.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV2,
    SecretReference,
)
from tests.product_config.conftest import write_config


def test_loads_strict_private_snapshot(tmp_path: Path, config: ProductConfigV2) -> None:
    path = write_config(tmp_path / "config.json", config)

    snapshot = load_product_config(path)

    assert isinstance(snapshot, ProductConfigSnapshot)
    assert snapshot.config == config
    assert len(snapshot.source_sha256) == len(snapshot.config_sha256) == 64
    assert [item.profile_id for item in config.profile_chain()] == ["primary", "backup"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"spec_version":"harnessix.product-config/v2","spec_version":"x"}',
        b'{"spec_version":"harnessix.product-config/v2","unknown":true}',
        b'{"spec_version":"harnessix.product-config/v2","value":NaN}',
        b"[]",
        b"\xff",
        b"",
    ],
)
def test_rejects_malformed_or_ambiguous_json(body: bytes) -> None:
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(body)
    assert error.value.code in {"product_config_invalid", "product_config_size"}


def test_rejects_unknown_profile_cycle_and_attempt_overflow(config: ProductConfigV2) -> None:
    primary = config.profiles[1]
    backup = config.profiles[0]
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"profiles"}),
            profiles=(
                backup.model_copy(update={"fallback_profiles": ("primary",)}),
                primary,
            ),
        )
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"profiles"}),
            profiles=(
                backup,
                primary.model_copy(update={"fallback_profiles": ("missing",)}),
            ),
        )
    chain = tuple(
        ModelProfile(
            profile_id=f"profile{index}",
            provider_id="primary",
            model="gpt-test",
            capabilities=ModelCapabilities(),
            fallback_profiles=((f"profile{index + 1}",) if index < 6 else ()),
            max_attempts=5,
        )
        for index in range(7)
    )
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"active_profile", "profiles"}),
            active_profile="profile0",
            profiles=chain,
        )


def test_rejects_insecure_file_link_and_hardlink(tmp_path: Path, config: ProductConfigV2) -> None:
    if os.name != "posix":
        pytest.skip("POSIX权限与硬链接语义")
    path = write_config(tmp_path / "config.json", config)
    path.chmod(0o644)
    with pytest.raises(KernelError) as error:
        load_product_config(path)
    assert error.value.code == "product_config_permissions"
    path.chmod(0o600)
    linked = tmp_path / "linked.json"
    os.link(path, linked)
    with pytest.raises(KernelError) as error:
        load_product_config(path)
    assert error.value.code == "product_config_permissions"
    linked.unlink()
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(KernelError) as error:
        load_product_config(alias)
    assert error.value.code in {"product_config_permissions", "product_config_unavailable"}


def test_finite_float_is_accepted_but_wrong_order_is_rejected(config: ProductConfigV2) -> None:
    value = config.model_dump(mode="json")
    value["profiles"][1]["retry_delay_seconds"] = 0.25
    decoded = decode_product_config_bytes(json.dumps(value).encode())
    assert isinstance(decoded, ProductConfigV2)
    assert decoded.profiles[1].retry_delay_seconds == 0.25
    value["providers"].reverse()
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(json.dumps(value).encode())
    assert error.value.code == "product_config_invalid"


def test_json_strictness_rejects_scalar_coercion_and_resource_exhaustion(
    config: ProductConfigV2,
) -> None:
    value = config.model_dump(mode="json")
    value["profiles"][1]["max_attempts"] = "2"
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(json.dumps(value).encode())
    assert error.value.code == "product_config_invalid"

    deep: object = "leaf"
    for _ in range(40):
        deep = [deep]
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(json.dumps(deep).encode())
    assert error.value.code == "product_config_invalid"
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(json.dumps([None] * 20_001).encode())
    assert error.value.code == "product_config_invalid"
    with pytest.raises(KernelError) as error:
        decode_product_config_bytes(b" " * (256 * 1024 + 1))
    assert error.value.code == "product_config_size"


def test_fallback_capability_and_secret_version_mismatch_are_rejected(
    config: ProductConfigV2,
) -> None:
    backup, primary = config.profiles
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"profiles"}),
            profiles=(
                backup.model_copy(
                    update={
                        "capabilities": ModelCapabilities(
                            tool_calls=False,
                            parallel_tool_calls=False,
                        )
                    }
                ),
                primary.model_copy(
                    update={
                        "required_capabilities": ModelCapabilities(
                            tool_calls=True,
                            parallel_tool_calls=False,
                        )
                    }
                ),
            ),
        )

    provider = config.providers[1]
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"providers"}),
            providers=(
                config.providers[0],
                provider.model_copy(
                    update={
                        "credential": SecretReference(
                            name=provider.credential.name,
                            version="different",
                        )
                    }
                ),
            ),
        )


def test_rejects_multiple_secret_references_for_same_environment_variable(
    config: ProductConfigV2,
) -> None:
    backup, primary = config.secret_sources
    with pytest.raises(ValidationError):
        ProductConfigV2(
            **config.model_dump(exclude={"secret_sources"}),
            secret_sources=(
                backup,
                primary.model_copy(update={"environment_variable": backup.environment_variable}),
            ),
        )


def test_a_missing_file_and_a_failed_read_are_told_apart(
    tmp_path: Path, config: ProductConfigV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.10.0b39 Windows：几个进程同时首次启动，读配置偶尔失败，却提示“还没有配置模型”。"""
    from harnessix.product_config import codec
    from harnessix.product_config.preflight_configuration import _read
    from harnessix.product_config.preflight_support import PreflightRecorder

    with pytest.raises(KernelError) as missing:
        codec.read_product_config_bytes(tmp_path / "absent.json")
    assert missing.value.code == "product_config_missing"

    path = tmp_path / "config.json"
    write_config(path, config)
    attempts: list[int] = []
    original = codec._read_once

    def busy_twice(*args: object) -> bytes:
        attempts.append(1)
        if len(attempts) <= 2:  # 另一个进程正在替换同一目录里的文件
            raise KernelError("product_config_unavailable", "busy")
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(codec, "_read_once", busy_twice)
    assert codec.read_product_config_bytes(path) == path.read_bytes()
    assert len(attempts) == 3

    monkeypatch.setattr(codec, "_TRANSIENT_READ_SECONDS", 0.0)

    def always_busy(*_: object) -> bytes:
        raise KernelError("product_config_unavailable", "busy")

    monkeypatch.setattr(codec, "_read_once", always_busy)
    recorder = PreflightRecorder(lambda: 0.0)
    assert _read(path, recorder) is None
    (check,) = recorder.ordered()
    assert check.code == "product_config_unavailable"
    assert check.remediation_id == "retry_config_read"  # 不是run_configure
