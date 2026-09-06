from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.evals.catalog import (
    HistoricalCodingEval,
    historical_coding_eval,
    historical_coding_eval_ids,
)
from harnessix.evals.checks import historical_python_launcher, run_historical_checks
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.materializer import (
    MaterializedCodingEval,
    load_materialized_coding_eval,
    materialize_historical_coding_eval,
)

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "harnessix-openai-empty-incremental-call-id"
SOURCE_REVISION = "9f24961840fa704e7c7a344c648164d8afe793b7"
SOURCE_TREE_OID = "c3df320a023537c1e0a6931a758ac67940279658"
SOURCE_TREE_SHA256 = "d91bdff8b78e85222f16e00b44b6888c563c2986f27ca831b414930ec2623ba4"
CHANGED_PATH = Path("src/harnessix/models/_chat_stream.py")


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git 不可用")
    return Path(executable).resolve()


def command(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return result.stdout.strip()


def definition() -> HistoricalCodingEval:
    return historical_coding_eval(TASK_ID)


def materialize(tmp_path: Path, run_id: UUID | None = None) -> MaterializedCodingEval:
    return materialize_historical_coding_eval(
        ROOT,
        tmp_path,
        git_executable(),
        definition(),
        run_id or uuid4(),
    )


def test_catalog_pins_source_contract_and_checks() -> None:
    item = definition()

    assert historical_coding_eval_ids() == (TASK_ID,)
    assert item.task.repository.source_revision == SOURCE_REVISION
    assert item.source_tree_oid == SOURCE_TREE_OID
    assert item.task.repository.baseline_tree_sha256 == SOURCE_TREE_SHA256
    assert item.task.allowed_changed_paths == (CHANGED_PATH.as_posix(),)
    assert item.task.required_test_profiles == ("focused",)
    assert item.host_only_paths == (".env.example",)
    assert item.task.baseline_checks == item.task.behavior_checks == ("empty-id-behavior",)
    assert item.task.regression_checks == ("identity-guards",)
    assert item.check("empty-id-behavior").mode == "empty_id_behavior"
    with pytest.raises(KernelError) as error:
        historical_coding_eval("missing")
    assert error.value.code == "eval_task_not_found"
    with pytest.raises(KernelError) as error:
        item.check("missing")
    assert error.value.code == "eval_check_not_found"


def test_materializes_exact_private_one_commit_baseline_and_reopens_dirty_tree(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    first = materialize(tmp_path, run_id)
    manifest = first.manifest
    target = first.workspace / CHANGED_PATH

    assert first.run_root.stat().st_mode & 0o077 == 0
    assert (first.run_root / "materialization.json").stat().st_mode & 0o777 == 0o600
    assert manifest.status == "ready"
    assert manifest.source_revision == SOURCE_REVISION
    assert manifest.source_tree_oid == SOURCE_TREE_OID
    assert manifest.baseline_tree_sha256 == SOURCE_TREE_SHA256
    assert manifest.tracked_files == 240
    assert command(first.workspace, "rev-list", "--count", "HEAD") == "1"
    assert command(first.workspace, "rev-parse", "HEAD") == manifest.baseline_revision
    assert SOURCE_REVISION not in command(first.workspace, "rev-list", "--all")
    assert "if part.id is not None:" in target.read_text(encoding="utf-8")
    assert not (first.workspace / "tests/models/test_chat_empty_call_id.py").exists()

    target.write_text(
        target.read_text(encoding="utf-8") + "\n# eval-dirty-tree\n", encoding="utf-8"
    )
    reopened = materialize(tmp_path, run_id)
    loaded = load_materialized_coding_eval(tmp_path, git_executable(), definition(), run_id)
    assert reopened == loaded
    assert "# eval-dirty-tree" in (reopened.workspace / CHANGED_PATH).read_text(encoding="utf-8")

    (first.run_root / "materialization.json").chmod(0o644)
    with pytest.raises(KernelError) as error:
        load_materialized_coding_eval(tmp_path, git_executable(), definition(), run_id)
    assert error.value.code == "eval_materialization_manifest_invalid"


def test_existing_incomplete_run_is_rejected_without_overwrite(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    run_root.mkdir(mode=0o700)
    marker = run_root / "owner-marker"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(KernelError) as error:
        materialize(tmp_path, run_id)

    assert error.value.code == "eval_materialization_manifest_invalid"
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_manifest_symbolic_link_and_wrong_source_tree_are_rejected(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    run_root.mkdir(mode=0o700)
    (run_root / "workspace").mkdir(mode=0o700)
    target = tmp_path / "external-manifest.json"
    target.write_text("{}", encoding="utf-8")
    (run_root / "materialization.json").symlink_to(target)

    with pytest.raises(KernelError) as error:
        materialize(tmp_path, run_id)
    assert error.value.code == "eval_materialization_manifest_invalid"

    mismatched = replace(definition(), source_tree_oid="f" * 40)
    second_run_id = uuid4()
    with pytest.raises(KernelError) as error:
        materialize_historical_coding_eval(
            ROOT,
            tmp_path,
            git_executable(),
            mismatched,
            second_run_id,
        )
    assert error.value.code == "eval_source_tree_mismatch"
    assert not (tmp_path / str(second_run_id)).exists()

    with pytest.raises(KernelError) as error:
        materialize_historical_coding_eval(
            ROOT / "src", tmp_path, git_executable(), definition(), uuid4()
        )
    assert error.value.code == "eval_source_root_mismatch"

    with pytest.raises(KernelError) as error:
        materialize_historical_coding_eval(ROOT, ROOT, git_executable(), definition(), uuid4())
    assert error.value.code == "eval_materialization_path_invalid"


async def test_hidden_checks_detect_baseline_and_accept_minimal_fix(tmp_path: Path) -> None:
    item = definition()
    materialized = materialize(tmp_path)
    python = Path(sys.executable)

    baseline = await run_historical_checks(item, materialized, python, "baseline")
    before = await run_historical_checks(item, materialized, python, "final")
    assert [(result.check_id, result.passed, result.returncode) for result in baseline] == [
        ("empty-id-behavior", False, 1)
    ]
    assert [(result.check_id, result.passed) for result in before] == [
        ("empty-id-behavior", False),
        ("identity-guards", True),
    ]

    target = materialized.workspace / CHANGED_PATH
    body = target.read_text(encoding="utf-8")
    assert body.count("if part.id is not None:") == 1
    target.write_text(
        body.replace("if part.id is not None:", 'if part.id not in (None, ""):'),
        encoding="utf-8",
    )

    final = await run_historical_checks(item, materialized, python, "final")
    assert [(result.check_id, result.passed, result.returncode) for result in final] == [
        ("empty-id-behavior", True, 0),
        ("identity-guards", True, 0),
    ]
    assert all(len(result.output_sha256) == 64 for result in (*baseline, *before, *final))

    evidence = await collect_git_evidence(
        materialized.workspace,
        git_executable(),
        baseline_revision=materialized.manifest.baseline_revision,
        baseline_tree_sha256=materialized.manifest.baseline_tree_sha256,
    )
    assert evidence.head_revision == evidence.baseline_revision
    assert evidence.changed_paths == (CHANGED_PATH.as_posix(),)
    assert not evidence.staged_paths and not evidence.unsupported_change_paths

    cancelled = CancelToken()
    cancelled.cancel()
    with pytest.raises(TurnCancelled):
        await run_historical_checks(item, materialized, python, "baseline", cancelled)


async def test_checker_exit_other_than_zero_or_one_is_infrastructure_failure(
    tmp_path: Path,
) -> None:
    materialized = materialize(tmp_path)
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_python.chmod(0o700)

    with pytest.raises(KernelError) as error:
        await run_historical_checks(
            definition(), materialized, fake_python, "baseline", CancelToken()
        )
    assert error.value.code == "eval_check_infrastructure_failed"


async def test_relative_python_binding_is_rejected(tmp_path: Path) -> None:
    materialized = materialize(tmp_path)

    with pytest.raises(KernelError) as error:
        await run_historical_checks(
            definition(), materialized, Path("python"), "baseline", CancelToken()
        )
    assert error.value.code == "eval_python_binding_invalid"

    external = tmp_path / "external-launcher"
    external.write_text("do-not-read", encoding="utf-8")
    host = materialized.run_root / "host"
    host.mkdir()
    (host / "python").symlink_to(external)
    with pytest.raises(KernelError) as error:
        await run_historical_checks(
            definition(), materialized, Path(sys.executable), "baseline", CancelToken()
        )
    assert error.value.code == "eval_python_launcher_failed"


def test_existing_python_launcher_is_verified_without_changing_identity(tmp_path: Path) -> None:
    materialized = materialize(tmp_path)
    first = historical_python_launcher(materialized, Path(sys.executable))
    identity = first.stat().st_ctime_ns

    assert historical_python_launcher(materialized, Path(sys.executable)) == first
    assert first.stat().st_ctime_ns == identity

    first.chmod(0o744)
    with pytest.raises(KernelError) as error:
        historical_python_launcher(materialized, Path(sys.executable))
    assert error.value.code == "eval_python_launcher_failed"
