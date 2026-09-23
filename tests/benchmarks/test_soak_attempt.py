from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from scripts.soak_attempt import (
    FINAL_FILENAME,
    STARTED_FILENAME,
    attempt_scope,
    begin_attempt,
    finish_attempt,
    read_attempt,
)
from scripts.soak_evidence import publish_run
from tests.benchmarks.test_soak_evidence import _input


def test_started_only_is_incomplete_and_failure_is_durable(tmp_path) -> None:
    manifest, _ = _input()
    root = tmp_path / "evidence"
    directory = begin_attempt(
        root,
        run_id=manifest.run_id,
        code_revision=manifest.code_revision,
        scenario_id=manifest.scenario_id,
    )

    started, final = read_attempt(directory)
    assert started.run_id == manifest.run_id
    assert final is None
    assert {path.name for path in directory.iterdir()} == {STARTED_FILENAME}

    finish_attempt(directory, outcome="failed", phase="measuring")
    _, final = read_attempt(directory)
    assert final is not None and final.outcome == "failed" and final.phase == "measuring"
    assert {path.name for path in directory.iterdir()} == {STARTED_FILENAME, FINAL_FILENAME}
    with pytest.raises(KernelError):
        finish_attempt(directory, outcome="failed", phase="measuring")


def test_scope_commits_only_after_run_revalidation(tmp_path) -> None:
    manifest, samples = _input()
    root = tmp_path / "evidence"

    with attempt_scope(
        root,
        run_id=manifest.run_id,
        code_revision=manifest.code_revision,
        scenario_id=manifest.scenario_id,
    ) as attempt:
        attempt.phase = "measuring"
        _, pending = read_attempt(attempt.directory)
        assert pending is None
        run_directory, digest = publish_run(root, manifest, samples)
        attempt.commit(run_directory)

    _, final = read_attempt(root / "attempts" / manifest.run_id)
    assert final is not None
    assert final.outcome == "committed"
    assert final.manifest_sha256 == digest
    with pytest.raises(KernelError) as error:
        begin_attempt(
            root,
            run_id=manifest.run_id,
            code_revision=manifest.code_revision,
            scenario_id=manifest.scenario_id,
        )
    assert error.value.code == "soak_attempt_exists"

    terminal = root / "attempts" / manifest.run_id / FINAL_FILENAME
    terminal.write_bytes(terminal.read_bytes().replace(b'"committed"', b'"failed"'))
    with pytest.raises(KernelError) as error:
        read_attempt(root / "attempts" / manifest.run_id)
    assert error.value.code == "soak_attempt_invalid"


def test_scope_failure_does_not_publish_or_erase_start(tmp_path) -> None:
    manifest, _ = _input()
    root = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="受控中断"):
        with attempt_scope(
            root,
            run_id=manifest.run_id,
            code_revision=manifest.code_revision,
            scenario_id=manifest.scenario_id,
        ) as attempt:
            attempt.phase = "warming"
            raise RuntimeError("受控中断")

    _, final = read_attempt(root / "attempts" / manifest.run_id)
    assert final is not None and final.outcome == "failed" and final.phase == "warming"
    assert not (root / manifest.run_id).exists()


def test_tampered_attempt_and_conflicting_run_fail_closed(tmp_path) -> None:
    manifest, samples = _input()
    root = tmp_path / "evidence"
    directory = begin_attempt(
        root,
        run_id=manifest.run_id,
        code_revision=manifest.code_revision,
        scenario_id=manifest.scenario_id,
    )
    publish_run(root, manifest, samples)
    with pytest.raises(KernelError) as error:
        finish_attempt(directory, outcome="failed", phase="measuring")
    assert error.value.code == "soak_attempt_invalid"
    (directory / STARTED_FILENAME).write_bytes(b"{}\n")
    with pytest.raises(KernelError) as error:
        read_attempt(directory)
    assert error.value.code == "soak_attempt_invalid"


def test_hard_exit_leaves_started_only_fact(tmp_path) -> None:
    manifest, _ = _input()
    root = tmp_path / "evidence"
    script = (
        "import os, sys; from pathlib import Path; "
        "from scripts.soak_attempt import begin_attempt; "
        "begin_attempt(Path(sys.argv[1]), run_id=sys.argv[2], "
        "code_revision=sys.argv[3], scenario_id=sys.argv[4]); os._exit(97)"
    )
    stopped = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            manifest.run_id,
            manifest.code_revision,
            manifest.scenario_id,
        ],
        cwd=Path(__file__).parents[2],
        check=False,
        timeout=30,
    )
    assert stopped.returncode == 97
    _, final = read_attempt(root / "attempts" / manifest.run_id)
    assert final is None
    assert not (root / manifest.run_id).exists()


def test_run_published_before_terminal_attempt_remains_incomplete(tmp_path) -> None:
    manifest, samples = _input()
    root = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="提交窗口中断"):
        with attempt_scope(
            root,
            run_id=manifest.run_id,
            code_revision=manifest.code_revision,
            scenario_id=manifest.scenario_id,
        ):
            publish_run(root, manifest, samples)
            raise RuntimeError("提交窗口中断")

    assert (root / manifest.run_id / "COMMITTED.json").exists()
    _, final = read_attempt(root / "attempts" / manifest.run_id)
    assert final is None
