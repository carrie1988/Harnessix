"""Action恢复正式基线入口的固定负载与复核回归。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_action_recovery_soak_release


def _manifest(revision: str) -> SimpleNamespace:
    return SimpleNamespace(
        code_revision=revision,
        scenario_id="action_recovery",
        status="baseline",
        platform="linux",
        run_id="b" * 32,
        load=SimpleNamespace(fault_matrix_version="action-recovery-v1", warmup_count=2),
        sample_counts={"recovery_scan": 20, "rss_peak": 1},
        fault_counts=SimpleNamespace(unknown_effect=44, duplicate_effect=0, orphan=0),
    )


async def test_release_uses_fixed_load_and_rechecks_both_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    manifest = _manifest(revision)
    digest = "c" * 64
    recorded: dict[str, object] = {}

    async def run(root: Path, **kwargs: object):
        assert root == tmp_path
        recorded.update(kwargs)
        return tmp_path / manifest.run_id, manifest

    monkeypatch.setattr(run_action_recovery_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_action_recovery_soak_release, "run_action_recovery", run)
    monkeypatch.setattr(
        run_action_recovery_soak_release,
        "read_published_run",
        lambda path: (manifest, digest),
    )
    monkeypatch.setattr(
        run_action_recovery_soak_release,
        "read_attempt",
        lambda path: (None, SimpleNamespace(outcome="committed", manifest_sha256=digest)),
    )

    result = await run_action_recovery_soak_release.run_release(tmp_path)

    assert recorded == {
        "code_revision": revision,
        "cycle_count": 20,
        "warmup_count": 2,
        "scan_timeout_seconds": 30,
    }
    assert result == {
        "scenario_id": "action_recovery",
        "platform": "linux",
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": digest,
        "status": "baseline",
    }


async def test_release_rejects_inconsistent_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    manifest = _manifest(revision)

    async def run(root: Path, **kwargs: object):
        return tmp_path / manifest.run_id, manifest

    monkeypatch.setattr(run_action_recovery_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_action_recovery_soak_release, "run_action_recovery", run)
    monkeypatch.setattr(
        run_action_recovery_soak_release,
        "read_published_run",
        lambda path: (manifest, "c" * 64),
    )
    monkeypatch.setattr(
        run_action_recovery_soak_release,
        "read_attempt",
        lambda path: (None, SimpleNamespace(outcome="committed", manifest_sha256="d" * 64)),
    )

    from harnessix.agent.errors import KernelError

    with pytest.raises(KernelError) as caught:
        await run_action_recovery_soak_release.run_release(tmp_path)
    assert caught.value.code == "soak_run_invalid"
