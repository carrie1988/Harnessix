from __future__ import annotations

import json
import subprocess
import sys
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
    assert (
        run_many_threads_soak_release.main(["--internal-worker", "--evidence-root", str(tmp_path)])
        == 1
    )
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
    assert (
        run_many_threads_soak_release.main(["--internal-worker", "--evidence-root", str(tmp_path)])
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "baseline"
    assert str(tmp_path) not in captured.out


def test_parent_runs_fixed_worker_with_hard_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: dict[str, object] = {}
    result = {
        "scenario_id": "many_threads",
        "platform": "linux",
        "code_revision": "a" * 40,
        "run_id": "b" * 32,
        "manifest_sha256": "c" * 64,
        "status": "baseline",
    }

    def worker(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        recorded["command"] = command
        recorded.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

    monkeypatch.setattr(run_many_threads_soak_release.subprocess, "run", worker)
    assert run_many_threads_soak_release.main(["--evidence-root", str(tmp_path)]) == 0
    assert recorded["command"] == (
        sys.executable,
        "-m",
        "scripts.run_many_threads_soak_release",
        "--internal-worker",
        "--evidence-root",
        str(tmp_path),
    )
    assert recorded["timeout"] == 20 * 60
    assert recorded["capture_output"] is True
    assert recorded["check"] is False
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result
    assert captured.err == ""


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (1, "", "多Thread Soak失败：soak_list_timeout\n", "soak_list_timeout"),
        (1, "", "private=/secret/path", "soak_worker_failed"),
        (0, "private=/secret/path", "", "soak_worker_invalid"),
        (0, "x" * 4097, "", "soak_worker_invalid"),
        (0, '{"platform":[]}', "", "soak_worker_invalid"),
        (
            0,
            json.dumps(
                {
                    "scenario_id": "many_threads",
                    "platform": "linux",
                    "code_revision": "a" * 40,
                    "run_id": "b" * 32,
                    "manifest_sha256": "c" * 64,
                    "status": "baseline",
                }
            ),
            "private=/secret/path",
            "soak_worker_invalid",
        ),
    ],
)
def test_parent_never_echoes_untrusted_worker_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        run_many_threads_soak_release.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )
    assert run_many_threads_soak_release.main(["--evidence-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"多Thread Soak失败：{expected}\n"
    assert str(tmp_path) not in captured.err


def test_parent_timeout_preserves_failure_classification_without_private_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("worker", 20 * 60, stderr=b"private=/secret/path")

    monkeypatch.setattr(run_many_threads_soak_release.subprocess, "run", timeout)
    assert run_many_threads_soak_release.main(["--evidence-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "多Thread Soak失败：soak_worker_timeout\n"


def test_parent_really_kills_stalled_worker_and_keeps_started_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "scripts"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "run_many_threads_soak_release.py").write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[-1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "(root / 'STARTED.json').write_text('started', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_many_threads_soak_release, "_REPOSITORY", tmp_path)
    monkeypatch.setattr(run_many_threads_soak_release, "_WORKER_TIMEOUT_SECONDS", 5)
    evidence_root = tmp_path / "evidence"

    assert run_many_threads_soak_release.main(["--evidence-root", str(evidence_root)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "多Thread Soak失败：soak_worker_timeout\n"
    assert (evidence_root / "STARTED.json").read_text(encoding="utf-8") == "started"
