from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_sdk_soak_release


async def test_release_entry_uses_fixed_formal_load_and_rechecks_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    run_id = "b" * 32
    digest = "c" * 64
    manifest = SimpleNamespace(
        code_revision=revision,
        status="baseline",
        platform="linux",
        run_id=run_id,
    )
    recorded: dict[str, object] = {}

    async def run(root: Path, **kwargs: object):
        recorded.update(kwargs)
        assert root == tmp_path
        return tmp_path / run_id, manifest

    monkeypatch.setattr(run_sdk_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_sdk_soak_release, "run_sdk_capacity", run)
    monkeypatch.setattr(
        run_sdk_soak_release,
        "read_published_run",
        lambda path: (manifest, digest),
    )
    monkeypatch.setattr(
        run_sdk_soak_release,
        "read_attempt",
        lambda path: (
            None,
            SimpleNamespace(outcome="committed", manifest_sha256=digest),
        ),
    )

    result = await run_sdk_soak_release.run_release(tmp_path)

    assert recorded == {
        "code_revision": revision,
        "measured_rounds": 3,
        "warmup_count": 1,
        "roundtrip_count": 20,
        "pending_limit": 64,
        "timeout_seconds": 30,
    }
    assert result == {
        "scenario_id": "sdk_capacity",
        "platform": "linux",
        "code_revision": revision,
        "run_id": run_id,
        "manifest_sha256": digest,
        "status": "baseline",
    }


@pytest.mark.parametrize("failure", ["missing_final", "wrong_revision", "wrong_status"])
async def test_release_entry_rejects_incomplete_or_nonbaseline_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    revision = "a" * 40
    manifest = SimpleNamespace(
        code_revision="b" * 40 if failure == "wrong_revision" else revision,
        status="unverified" if failure == "wrong_status" else "baseline",
        platform="linux",
        run_id="c" * 32,
    )

    async def run(*_args: object, **_kwargs: object):
        return tmp_path / manifest.run_id, manifest

    monkeypatch.setattr(run_sdk_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_sdk_soak_release, "run_sdk_capacity", run)
    monkeypatch.setattr(
        run_sdk_soak_release, "read_published_run", lambda path: (manifest, "d" * 64)
    )
    monkeypatch.setattr(
        run_sdk_soak_release,
        "read_attempt",
        lambda path: (
            None,
            None
            if failure == "missing_final"
            else SimpleNamespace(outcome="committed", manifest_sha256="d" * 64),
        ),
    )

    with pytest.raises(KernelError) as rejected:
        await run_sdk_soak_release.run_release(tmp_path)
    assert rejected.value.code == "soak_run_invalid"


def test_release_cli_does_not_print_private_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(_root: Path):
        raise KernelError("soak_sdk_timeout", f"private={tmp_path}/secret")

    monkeypatch.setattr(run_sdk_soak_release, "run_release", fail)
    assert run_sdk_soak_release.main(["--evidence-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "SDK Soak失败：soak_sdk_timeout\n"
    assert str(tmp_path) not in captured.err


def test_release_cli_success_prints_only_whitelisted_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def finish(_root: Path):
        return {
            "scenario_id": "sdk_capacity",
            "platform": "linux",
            "code_revision": "a" * 40,
            "run_id": "b" * 32,
            "manifest_sha256": "c" * 64,
            "status": "baseline",
        }

    monkeypatch.setattr(run_sdk_soak_release, "run_release", finish)
    assert run_sdk_soak_release.main(["--evidence-root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "baseline"
    assert str(tmp_path) not in captured.out
