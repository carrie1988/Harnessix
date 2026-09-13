from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import (
    ProductConfigSnapshot,
    product_config_digest,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import (
    ConfigurationWriteRequest,
    _open_lock,
    build_product_config,
    write_product_config,
)


def _draft(*, model: str = "gpt-test") -> ConfigurationDraft:
    return ConfigurationDraft(
        provider_kind="openai_chat",
        base_url="https://api.openai.test/v1",
        model=model,
        environment_variable="PRIMARY_API_KEY",
        output_token_parameter="max_completion_tokens",
    )


def test_builds_one_provider_config_without_reading_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "sk-secret-value-must-not-leak"
    monkeypatch.setenv("PRIMARY_API_KEY", canary)

    config = build_product_config(_draft())

    assert config.active_profile == "primary"
    assert config.providers[0].credential == config.secret_sources[0].secret
    assert config.secret_sources[0].environment_variable == "PRIMARY_API_KEY"
    assert canary not in config.model_dump_json()


def test_creates_private_canonical_config_and_self_validating_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "config.json"
    request = ConfigurationWriteRequest(path=path, draft=_draft())

    receipt = write_product_config(request)

    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    assert receipt.operation == "created"
    assert receipt.previous_source_sha256 is None
    assert receipt.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt.config_sha256 == product_config_digest(loaded.config)
    assert json.loads(path.read_text(encoding="utf-8"))["spec_version"] == (
        "harnessix.product-config/v2"
    )
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_create_refuses_existing_target_and_replace_requires_digest(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))

    with pytest.raises(KernelError) as exists:
        write_product_config(ConfigurationWriteRequest(path=path, draft=_draft(model="gpt-next")))
    assert exists.value.code == "product_config_exists"

    with pytest.raises(KernelError) as missing_digest:
        write_product_config(
            ConfigurationWriteRequest(path=path, draft=_draft(model="gpt-next"), replace=True)
        )
    assert missing_digest.value.code == "product_config_expected_digest_required"


def test_replaces_with_exact_cas_and_rejects_stale_writer(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    created = write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    replaced = write_product_config(
        ConfigurationWriteRequest(
            path=path,
            draft=_draft(model="gpt-next"),
            expected_source_sha256=created.source_sha256,
            replace=True,
        )
    )

    assert replaced.operation == "replaced"
    assert replaced.previous_source_sha256 == created.source_sha256
    assert replaced.source_sha256 != created.source_sha256
    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    assert loaded.config.profiles[0].model == "gpt-next"
    replaced_bytes = path.read_bytes()

    with pytest.raises(KernelError) as stale:
        write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(model="gpt-stale"),
                expected_source_sha256=created.source_sha256,
                replace=True,
            )
        )
    assert stale.value.code == "product_config_conflict"
    assert path.read_bytes() == replaced_bytes


def test_lock_contention_is_retryable_and_does_not_write_target(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    lock = tmp_path / ".config.json.configure.lock"
    descriptor = _open_lock(lock)
    try:
        with pytest.raises(KernelError) as busy:
            write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
        assert busy.value.code == "product_config_lock_timeout"
        assert busy.value.retryable is True
        assert not path.exists()
    finally:
        os.close(descriptor)


def test_concurrent_creators_allow_exactly_one_commit(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    temporary_ready = Event()
    release_writer = Event()

    def pause_after_temporary_sync(point: str) -> None:
        if point == "configuration.after_temporary_fsync":
            temporary_ready.set()
            assert release_writer.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            write_product_config,
            ConfigurationWriteRequest(path=path, draft=_draft()),
            fault=pause_after_temporary_sync,
        )
        assert temporary_ready.wait(timeout=5)
        second = pool.submit(
            write_product_config,
            ConfigurationWriteRequest(path=path, draft=_draft(model="gpt-second")),
        )
        with pytest.raises(KernelError) as blocked:
            second.result(timeout=5)
        release_writer.set()
        receipt = first.result(timeout=5)

    assert blocked.value.code == "product_config_lock_timeout"
    assert receipt.operation == "created"
    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    assert loaded.config.profiles[0].model == "gpt-test"


def test_rejects_linked_parent_and_lock(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX符号链接和硬链接身份语义")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(KernelError) as linked_parent:
        write_product_config(ConfigurationWriteRequest(path=alias / "config.json", draft=_draft()))
    assert linked_parent.value.code == "product_config_permissions"

    path = private / "config.json"
    lock = private / ".config.json.configure.lock"
    unrelated = private / "unrelated"
    unrelated.write_bytes(b"\0")
    unrelated.chmod(0o600)
    os.link(unrelated, lock)
    with pytest.raises(KernelError) as linked_lock:
        write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    assert linked_lock.value.code == "product_config_lock"
    assert not path.exists()


def test_failure_before_commit_preserves_old_bytes_and_cleans_temporary(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    created = write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    original = path.read_bytes()

    def fail(point: str) -> None:
        if point == "configuration.after_temporary_fsync":
            raise RuntimeError("injected")

    with pytest.raises(KernelError) as failed:
        write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(model="gpt-next"),
                expected_source_sha256=created.source_sha256,
                replace=True,
            ),
            fault=fail,
        )
    assert failed.value.code == "product_config_write_failed"
    assert path.read_bytes() == original
    assert not tuple(tmp_path.glob(".config.json.*.tmp"))


@pytest.mark.parametrize("failure_point", ["temporary_write", "atomic_replace"])
def test_io_failure_before_replace_preserves_old_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import harnessix.product_config.wizard as wizard

    path = tmp_path / "config.json"
    created = write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    original = path.read_bytes()
    if failure_point == "temporary_write":
        monkeypatch.setattr(
            wizard,
            "_write_private_file",
            lambda *_: (_ for _ in ()).throw(OSError("injected")),
        )
    else:
        monkeypatch.setattr(
            wizard.os,
            "replace",
            lambda *_: (_ for _ in ()).throw(OSError("injected")),
        )

    with pytest.raises(KernelError) as failed:
        write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(model="gpt-next"),
                expected_source_sha256=created.source_sha256,
                replace=True,
            )
        )
    assert failed.value.code == "product_config_write_failed"
    assert path.read_bytes() == original


def test_directory_sync_failure_after_replace_reports_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harnessix.product_config.wizard as wizard

    path = tmp_path / "config.json"
    created = write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    monkeypatch.setattr(
        wizard,
        "_sync_directory",
        lambda *_: (_ for _ in ()).throw(OSError("injected")),
    )

    with pytest.raises(KernelError) as unknown:
        write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(model="gpt-next"),
                expected_source_sha256=created.source_sha256,
                replace=True,
            )
        )
    assert unknown.value.code == "product_config_commit_unknown"
    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    assert loaded.config.profiles[0].model == "gpt-next"


def test_failure_after_atomic_commit_reports_unknown_without_retrying(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    created = write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))

    def fail(point: str) -> None:
        if point == "configuration.after_replace_fsync":
            raise RuntimeError("injected")

    with pytest.raises(KernelError) as unknown:
        write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(model="gpt-next"),
                expected_source_sha256=created.source_sha256,
                replace=True,
            ),
            fault=fail,
        )
    assert unknown.value.code == "product_config_commit_unknown"
    loaded = load_product_config(path)
    assert isinstance(loaded, ProductConfigSnapshot)
    assert loaded.config.profiles[0].model == "gpt-next"


def test_reopen_verification_failure_reports_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harnessix.product_config.wizard as wizard

    path = tmp_path / "config.json"
    monkeypatch.setattr(
        wizard,
        "load_product_config",
        lambda _: (_ for _ in ()).throw(KernelError("product_config_invalid", "invalid")),
    )
    with pytest.raises(KernelError) as unknown:
        write_product_config(ConfigurationWriteRequest(path=path, draft=_draft()))
    assert unknown.value.code == "product_config_commit_unknown"
    assert path.exists()


def test_receipt_never_contains_configuration_or_secret_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "sk-secret-value-must-not-leak"
    monkeypatch.setenv("PRIMARY_API_KEY", canary)
    receipt = write_product_config(
        ConfigurationWriteRequest(path=tmp_path / "config.json", draft=_draft())
    )
    encoded = receipt.model_dump_json()
    assert canary not in encoded
    assert "api.openai.test" not in encoded
    assert "PRIMARY_API_KEY" not in encoded
    assert "gpt-test" not in encoded
