"""Artifact增长正式基线入口的固定负载与复核回归。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_artifact_growth_soak_release

_ZERO_FAULTS = {
    "cancelled": 0,
    "timed_out": 0,
    "eof": 0,
    "unknown_effect": 0,
    "duplicate_effect": 0,
    "orphan": 0,
}


def _manifest(revision: str) -> SimpleNamespace:
    return SimpleNamespace(
        code_revision=revision,
        scenario_id="artifact_growth",
        status="baseline",
        spec_version="harnessix.soak-manifest/v3",
        platform="linux",
        run_id="b" * 32,
        load=SimpleNamespace(warmup_count=2, artifact_count=302),
        sample_counts={"artifact_publish": 300, "artifact_read": 390, "rss_peak": 1},
        fault_counts=SimpleNamespace(model_dump=lambda: dict(_ZERO_FAULTS)),
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

    monkeypatch.setattr(run_artifact_growth_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_artifact_growth_soak_release, "run_artifact_growth", run)
    monkeypatch.setattr(
        run_artifact_growth_soak_release, "read_published_run", lambda path: (manifest, digest)
    )
    monkeypatch.setattr(
        run_artifact_growth_soak_release,
        "read_attempt",
        lambda path: (None, SimpleNamespace(outcome="committed", manifest_sha256=digest)),
    )

    result = await run_artifact_growth_soak_release.run_release(tmp_path)

    assert recorded == {
        "code_revision": revision,
        "turn_count": 300,
        "warmup_count": 2,
        "seed": 0,
    }
    assert result == {
        "scenario_id": "artifact_growth",
        "platform": "linux",
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": digest,
        "status": "baseline",
    }


async def test_release_rejects_shrunk_publish_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    manifest = _manifest(revision)
    manifest.sample_counts = {"artifact_publish": 19, "artifact_read": 26, "rss_peak": 1}

    async def run(root: Path, **kwargs: object):
        return tmp_path / manifest.run_id, manifest

    monkeypatch.setattr(run_artifact_growth_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_artifact_growth_soak_release, "run_artifact_growth", run)
    monkeypatch.setattr(
        run_artifact_growth_soak_release,
        "read_published_run",
        lambda path: (manifest, "c" * 64),
    )
    monkeypatch.setattr(
        run_artifact_growth_soak_release,
        "read_attempt",
        lambda path: (None, SimpleNamespace(outcome="committed", manifest_sha256="c" * 64)),
    )

    with pytest.raises(KernelError) as caught:
        await run_artifact_growth_soak_release.run_release(tmp_path)
    assert caught.value.code == "soak_run_invalid"
