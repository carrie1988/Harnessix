from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.leases import WorkspaceLeaseStore

WORKSPACE = "a" * 64


def test_cross_process_lease_uses_monotonic_fencing_tokens(tmp_path: Path) -> None:
    now = [100.0]
    first = WorkspaceLeaseStore(tmp_path / "private/leases.db", clock=lambda: now[0])
    second = WorkspaceLeaseStore(tmp_path / "private/leases.db", clock=lambda: now[0])
    try:
        lease = first.acquire(WORKSPACE, "owner-a", ttl_seconds=10)
        with pytest.raises(KernelError) as busy:
            second.acquire(WORKSPACE, "owner-b", ttl_seconds=10)
        assert busy.value.code == "workspace_busy"

        now[0] = 111.0
        replacement = second.acquire(WORKSPACE, "owner-b", ttl_seconds=10)
        assert replacement.fencing_token == lease.fencing_token + 1
        with pytest.raises(KernelError) as stale:
            first.assert_current(lease)
        assert stale.value.code == "workspace_lease_lost"
        second.assert_current(replacement)
    finally:
        first.close()
        second.close()


def test_renew_and_release_do_not_reuse_old_fence(tmp_path: Path) -> None:
    now = [100.0]
    with WorkspaceLeaseStore(tmp_path / "leases.db", clock=lambda: now[0]) as store:
        lease = store.acquire(WORKSPACE, "owner", ttl_seconds=5)
        renewed = store.renew(lease, ttl_seconds=10)
        with pytest.raises(KernelError):
            store.assert_current(lease)
        store.assert_current(renewed)
        store.release(renewed)
        replacement = store.acquire(WORKSPACE, "owner", ttl_seconds=10)
        assert replacement.fencing_token == renewed.fencing_token + 1


def test_lease_is_enforced_by_an_independent_process(tmp_path: Path) -> None:
    path = tmp_path / "private/leases.db"
    with WorkspaceLeaseStore(path) as store:
        store.acquire(WORKSPACE, "parent", ttl_seconds=60)
        program = """
import sys
from harnessix.agent.errors import KernelError
from harnessix.workspace.leases import WorkspaceLeaseStore
try:
    with WorkspaceLeaseStore(sys.argv[1]) as store:
        store.acquire('a' * 64, 'child', ttl_seconds=60)
except KernelError as error:
    print(error.code)
"""
        completed = subprocess.run(
            [sys.executable, "-c", program, str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "workspace_busy"
    assert completed.stderr == ""
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_lease_rejects_invalid_ttl(tmp_path: Path, ttl: float) -> None:
    with WorkspaceLeaseStore(tmp_path / "leases.db") as store:
        with pytest.raises(KernelError) as error:
            store.acquire(WORKSPACE, "owner", ttl_seconds=ttl)
    assert error.value.code == "workspace_lease_invalid"
