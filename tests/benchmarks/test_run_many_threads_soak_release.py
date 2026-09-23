from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_many_threads_soak_release


def _manifest(revision: str) -> SimpleNamespace:
    return SimpleNamespace(
        code_revision=revision,
        scenario_id="many_threads",
        status="baseline",
        platform="linux",
        run_id="b" * 32,
        load=SimpleNamespace(thread_count=500, warmup_count=11),
        sample_counts={"app_service_startup": 3, "thread_list_page": 30, "rss_peak": 1},
        provider=SimpleNamespace(request_count=0),
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

    monkeypatch.setattr(run_many_threads_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_many_threads_soak_release, "run_many_threads", run)
    monkeypatch.setattr(
        run_many_threads_soak_release, "read_published_run", lambda path: (manifest, digest)
    )
    monkeypatch.setattr(
        run_many_threads_soak_release,
        "read_attempt",
        lambda path: (None, SimpleNamespace(outcome="committed", manifest_sha256=digest)),
    )

    result = await run_many_threads_soak_release.run_release(tmp_path)

    assert recorded == {
        "code_revision": revision,
        "thread_count": 500,
        "list_limit": 50,
        "restart_count": 3,
        "startup_timeout_seconds": 120,
        "page_timeout_seconds": 120,
    }
    assert result == {
        "scenario_id": "many_threads",
        "platform": "linux",
        "code_revision": revision,
        "run_id": manifest.run_id,
        "manifest_sha256": digest,
        "status": "baseline",
    }


@pytest.mark.parametrize(
    "failure",
    [
        "missing_final",
        "wrong_revision",
        "wrong_status",
        "wrong_load",
        "missing_page",
        "model_request",
        "wrong_digest",
        "wrong_reader_manifest",
    ],
)
async def test_release_rejects_invalid_formal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    revision = "a" * 40
    manifest = _manifest(revision)
    digest = "c" * 64
    if failure == "wrong_revision":
        manifest.code_revision = "d" * 40
    elif failure == "wrong_status":
        manifest.status = "unverified"
    elif failure == "wrong_load":
        manifest.load.thread_count = 499
    elif failure == "missing_page":
        manifest.sample_counts["thread_list_page"] = 29
    elif failure == "model_request":
        manifest.provider.request_count = 1

    async def run(*_args: object, **_kwargs: object):
        return tmp_path / manifest.run_id, manifest

    monkeypatch.setattr(run_many_threads_soak_release, "_revision", lambda: revision)
    monkeypatch.setattr(run_many_threads_soak_release, "run_many_threads", run)
    monkeypatch.setattr(
        run_many_threads_soak_release,
        "read_published_run",
        lambda path: (object() if failure == "wrong_reader_manifest" else manifest, digest),
    )
    monkeypatch.setattr(
        run_many_threads_soak_release,
        "read_attempt",
        lambda path: (
            None,
            None
            if failure == "missing_final"
            else SimpleNamespace(
                outcome="committed",
                manifest_sha256="d" * 64 if failure == "wrong_digest" else digest,
            ),
        ),
    )

    with pytest.raises(KernelError) as rejected:
        await run_many_threads_soak_release.run_release(tmp_path)
    assert rejected.value.code == "soak_run_invalid"


def test_cli_redacts_private_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(_root: Path):
        raise KernelError("soak_list_timeout", f"private={tmp_path}/secret")

    monkeypatch.setattr(run_many_threads_soak_release, "run_release", fail)
    assert run_many_threads_soak_release.main(["--evidence-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "多Thread Soak失败：soak_list_timeout\n"
    assert str(tmp_path) not in captured.err


def test_cli_emits_only_public_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def finish(_root: Path):
        return {
            "scenario_id": "many_threads",
            "platform": "linux",
            "code_revision": "a" * 40,
            "run_id": "b" * 32,
            "manifest_sha256": "c" * 64,
            "status": "baseline",
        }

    monkeypatch.setattr(run_many_threads_soak_release, "run_release", finish)
    assert run_many_threads_soak_release.main(["--evidence-root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "baseline"
    assert str(tmp_path) not in captured.out
