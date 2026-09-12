from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.product_ui import ClientStateStore, ProductUIError


def _state_root(tmp_path: Path) -> Path:
    return tmp_path / "product-state"


def test_state_store_initializes_private_state_and_preserves_client_identity(
    tmp_path: Path,
) -> None:
    state_root = _state_root(tmp_path)
    workspace_identity = str(tmp_path / "workspace")

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as first:
        first_state = first.state()
        assert first_state.next_command_sequence == 1
        assert first_state.state_revision == 1
        assert first_state.thread_cursors == ()

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as second:
        second_state = second.state()
        assert second_state.client_instance_id == first_state.client_instance_id
        assert second_state.digest == first_state.digest

    if os.name == "posix":
        assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((state_root / "client-state.json").stat().st_mode) == 0o600


def test_command_ids_are_committed_before_return_and_never_reused(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    workspace_identity = str(tmp_path / "workspace")

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as store:
        first = store.allocate_command_id()
        second = store.allocate_command_id()
        assert first.sequence == 1
        assert second.sequence == 2
        assert first.request_id != second.request_id
        assert store.state().next_command_sequence == 3
        assert second.state_revision == store.state().state_revision

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as reopened:
        third = reopened.allocate_command_id()
        assert third.sequence == 3
        assert third.request_id.endswith("-3")


def test_state_store_persists_selection_and_monotonic_thread_cursor(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    workspace_identity = str(tmp_path / "workspace")
    thread_id = uuid4()

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as store:
        selected = store.select_thread(thread_id)
        advanced = store.advance_cursor(thread_id, 12)
        repeated = store.advance_cursor(thread_id, 12)
        stale = store.advance_cursor(thread_id, 7)

        assert selected.selected_thread_id == thread_id
        assert advanced.cursor_for(thread_id) == 12
        assert repeated.state_revision == advanced.state_revision
        assert stale.state_revision == advanced.state_revision

    with ClientStateStore(state_root, workspace_identity=workspace_identity) as reopened:
        assert reopened.state().selected_thread_id == thread_id
        assert reopened.state().cursor_for(thread_id) == 12


def test_state_store_rejects_workspace_mismatch(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    with ClientStateStore(state_root, workspace_identity="/workspace/one"):
        pass

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace/two")

    assert error.value.code == "client_state_workspace_mismatch"


def test_state_store_rejects_concurrent_writer(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    first = ClientStateStore(state_root, workspace_identity="/workspace")
    try:
        with pytest.raises(ProductUIError) as error:
            ClientStateStore(state_root, workspace_identity="/workspace")
        assert error.value.code == "client_state_busy"
        assert error.value.retryable
    finally:
        first.close()


def test_state_store_rejects_symbolic_lock_without_mutating_target(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    state_root.mkdir(mode=0o700)
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    (state_root / ".client-state.lock").symlink_to(target)

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace")

    assert error.value.code == "client_state_permissions"
    assert target.read_bytes() == b""


def test_state_store_rejects_symlink_and_corrupt_digest(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    state_root.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (state_root / "client-state.json").symlink_to(outside)

    with pytest.raises(ProductUIError) as linked:
        ClientStateStore(state_root, workspace_identity="/workspace")
    assert linked.value.code == "client_state_permissions"

    (state_root / "client-state.json").unlink()
    with ClientStateStore(state_root, workspace_identity="/workspace"):
        pass
    state_path = state_root / "client-state.json"
    payload = json.loads(state_path.read_bytes())
    payload["next_command_sequence"] = 9
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name == "posix":
        state_path.chmod(0o600)

    with pytest.raises(ProductUIError) as corrupt:
        ClientStateStore(state_root, workspace_identity="/workspace")
    assert corrupt.value.code == "client_state_corrupt"


@pytest.mark.skipif(os.name != "posix", reason="POSIX权限位专项断言")
def test_state_store_rejects_overly_broad_posix_permissions(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    with ClientStateStore(state_root, workspace_identity="/workspace"):
        pass
    (state_root / "client-state.json").chmod(0o644)

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace")
    assert error.value.code == "client_state_permissions"


def test_failed_atomic_replace_preserves_previous_command_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = _state_root(tmp_path)
    store = ClientStateStore(state_root, workspace_identity="/workspace")
    original_replace = os.replace

    def fail_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes], target: object
    ) -> None:
        del source, target
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        with pytest.raises(ProductUIError) as error:
            store.allocate_command_id()
        assert error.value.code == "client_state_write_failed"
        assert store.state().next_command_sequence == 1
    finally:
        monkeypatch.setattr(os, "replace", original_replace)
        store.close()

    with ClientStateStore(state_root, workspace_identity="/workspace") as reopened:
        allocation = reopened.allocate_command_id()
        assert allocation.sequence == 1


@pytest.mark.skipif(os.name != "posix", reason="目录fsync语义仅适用于POSIX实现")
def test_directory_sync_failure_never_reuses_replaced_command_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = _state_root(tmp_path)
    store = ClientStateStore(state_root, workspace_identity="/workspace")
    original_fsync = os.fsync
    calls = 0

    def fail_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    try:
        with pytest.raises(ProductUIError) as error:
            store.allocate_command_id()
        assert error.value.code == "client_state_write_failed"
    finally:
        monkeypatch.setattr(os, "fsync", original_fsync)

    assert store.state().next_command_sequence == 2
    allocation = store.allocate_command_id()
    assert allocation.sequence == 2
    store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX权限位专项断言")
def test_state_store_rejects_broad_lock_permissions_without_repair(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    with ClientStateStore(state_root, workspace_identity="/workspace"):
        pass
    lock_path = state_root / ".client-state.lock"
    lock_path.chmod(0o644)

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace")

    assert error.value.code == "client_state_permissions"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o644


def test_closed_state_store_rejects_operations(tmp_path: Path) -> None:
    store = ClientStateStore(_state_root(tmp_path), workspace_identity="/workspace")
    store.close()
    store.close()

    with pytest.raises(ProductUIError) as error:
        store.state()
    assert error.value.code == "client_state_closed"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda value: value.update(spec_version="harnessix.client-state/v2"),
            "client_state_version",
        ),
        (lambda value: value.update(unexpected=True), "client_state_corrupt"),
    ),
)
def test_state_store_rejects_unknown_version_and_fields(
    tmp_path: Path,
    mutation,
    expected_code: str,
) -> None:
    state_root = _state_root(tmp_path)
    with ClientStateStore(state_root, workspace_identity="/workspace"):
        pass
    state_path = state_root / "client-state.json"
    payload = json.loads(state_path.read_bytes())
    mutation(payload)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name == "posix":
        state_path.chmod(0o600)

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace")
    assert error.value.code == expected_code


def test_state_store_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    state_root = _state_root(tmp_path)
    with ClientStateStore(state_root, workspace_identity="/workspace"):
        pass
    state_path = state_root / "client-state.json"
    body = state_path.read_text(encoding="utf-8").rstrip()
    state_path.write_text(body[:-1] + ',"digest":"' + "0" * 64 + '"}', encoding="utf-8")
    if os.name == "posix":
        state_path.chmod(0o600)

    with pytest.raises(ProductUIError) as error:
        ClientStateStore(state_root, workspace_identity="/workspace")
    assert error.value.code == "client_state_corrupt"
