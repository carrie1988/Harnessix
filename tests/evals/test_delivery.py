from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import harnessix.evals.delivery as delivery_module
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.evals.catalog import historical_coding_eval
from harnessix.evals.checks import run_historical_checks
from harnessix.evals.contracts import EvalRepository
from harnessix.evals.delivery import (
    CodingEvalDeliveryStore,
    _new_package,
    build_coding_eval_change_package,
    read_coding_eval_change_package,
    write_coding_eval_change_package,
)
from harnessix.evals.delivery_contracts import CodingEvalChangePackage, change_image
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.materializer import load_materialized_coding_eval
from harnessix.tools.workspace import digest
from tests.evals.test_runner import ROOT, HistoricalFixProvider, git_executable, run

TASK_ID = "harnessix-openai-empty-incremental-call-id"
PATH = "src/example.py"
ORIGIN = "https://github.com/example/harnessix-delivery-fixture"


def git(root: Path, *arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=text,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return result.stdout.strip() if text else result.stdout


def repository(tmp_path: Path) -> tuple[Path, CodingEvalChangePackage]:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Harnessix Test")
    git(root, "config", "user.email", "test@harnessix.invalid")
    git(root, "remote", "add", "origin", ORIGIN)
    path = root / PATH
    path.parent.mkdir(parents=True)
    before = b"value = 1\n"
    after = b"value = 2\n"
    path.write_bytes(before)
    path.chmod(0o644)
    git(root, "add", "--all")
    git(root, "commit", "-q", "--no-gpg-sign", "-m", "baseline")
    revision = str(git(root, "rev-parse", "HEAD"))
    tree_oid = str(git(root, "rev-parse", "HEAD^{tree}"))
    tree = git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD", text=False)
    assert isinstance(tree, bytes)
    package = _new_package(
        run_id=uuid4(),
        task_id="delivery-fixture",
        task_version=1,
        task_fingerprint="a" * 64,
        report_sha256="b" * 64,
        repository=EvalRepository(
            name="fixture",
            origin=ORIGIN,
            source_revision=revision,
            baseline_tree_sha256=hashlib.sha256(tree).hexdigest(),
        ),
        source_tree_oid=tree_oid,
        path=PATH,
        source_mode=0o644,
        before=change_image(before),
        after=change_image(after),
        workspace_diff_sha256="c" * 64,
        created_at=datetime.now(UTC),
    )
    return root, package


def store(tmp_path: Path) -> CodingEvalDeliveryStore:
    return CodingEvalDeliveryStore(tmp_path / "deliveries", git_executable())


def decision(record, outcome: ApprovalOutcome = ApprovalOutcome.APPROVED) -> ApprovalRecord:
    return ApprovalRecord(
        outcome=outcome,
        actor="release-owner",
        reason="reviewed",
        request_fingerprint=record.plan.approval_fingerprint,
    )


def test_change_package_private_round_trip_and_tamper_rejection(tmp_path: Path) -> None:
    _, package = repository(tmp_path)
    path = tmp_path / "package.json"

    write_coding_eval_change_package(path, package)

    assert read_coding_eval_change_package(path) == package
    assert path.stat().st_mode & 0o777 == 0o600
    payload = package.model_dump(mode="json")
    payload["after"]["text"] = "tampered\n"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(KernelError) as error:
        read_coding_eval_change_package(path)
    assert error.value.code == "eval_change_package_invalid"


def test_explicit_approval_applies_once_and_preserves_mode(tmp_path: Path) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    pending = deliveries.prepare(package, root)
    approval = decision(pending)

    approved = deliveries.decide(pending.delivery_id, approval)
    applied = deliveries.execute(pending.delivery_id, root)
    repeated = deliveries.execute(pending.delivery_id, root)

    assert approved.status == "approved"
    assert applied == repeated and applied.status == "applied"
    assert (root / PATH).read_text(encoding="utf-8") == "value = 2\n"
    assert (root / PATH).stat().st_mode & 0o777 == 0o644
    assert [item.to_status for item in applied.transitions] == [
        "pending_approval",
        "approved",
        "applying",
        "applied",
    ]
    directory = deliveries.root / str(pending.delivery_id)
    assert (directory / "package.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "state.json").stat().st_mode & 0o777 == 0o600
    assert git(root, "status", "--porcelain") == f"M {PATH}"


def test_rejection_and_approval_binding_never_write_target(tmp_path: Path) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    first = deliveries.prepare(package, root)
    wrong = decision(first).model_copy(update={"request_fingerprint": "d" * 64})

    with pytest.raises(KernelError) as error:
        deliveries.decide(first.delivery_id, wrong)
    assert error.value.code == "eval_delivery_approval_mismatch"

    rejection = decision(first, ApprovalOutcome.REJECTED)
    rejected = deliveries.decide(first.delivery_id, rejection)
    assert rejected.status == "rejected"
    assert deliveries.decide(first.delivery_id, rejection) == rejected
    with pytest.raises(KernelError) as error:
        deliveries.decide(first.delivery_id, decision(first))
    assert error.value.code == "eval_delivery_approval_conflict"
    with pytest.raises(KernelError) as error:
        deliveries.execute(first.delivery_id, root)
    assert error.value.code == "eval_delivery_not_executable"
    assert (root / PATH).read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("dirty", ["unstaged", "staged", "untracked"])
def test_prepare_rejects_every_git_dirty_class(tmp_path: Path, dirty: str) -> None:
    root, package = repository(tmp_path)
    if dirty == "unstaged":
        (root / PATH).write_text("user = 1\n", encoding="utf-8")
    elif dirty == "staged":
        (root / PATH).write_text("user = 1\n", encoding="utf-8")
        git(root, "add", PATH)
    else:
        (root / "user.tmp").write_text("user", encoding="utf-8")

    with pytest.raises(KernelError) as error:
        store(tmp_path).prepare(package, root)
    assert error.value.code == "eval_delivery_target_dirty"


def test_prepare_rejects_source_revision_origin_and_file_type_drift(tmp_path: Path) -> None:
    root, package = repository(tmp_path)
    git(root, "remote", "set-url", "origin", "https://example.invalid/other")
    with pytest.raises(KernelError) as error:
        store(tmp_path).prepare(package, root)
    assert error.value.code == "eval_delivery_origin_mismatch"

    git(root, "remote", "set-url", "origin", ORIGIN)
    (root / "other").write_text("drift\n", encoding="utf-8")
    git(root, "add", "other")
    git(root, "commit", "-q", "--no-gpg-sign", "-m", "drift")
    with pytest.raises(KernelError) as error:
        store(tmp_path).prepare(package, root)
    assert error.value.code == "eval_delivery_source_drift"

    git(root, "reset", "--hard", package.repository.source_revision)
    (root / PATH).unlink()
    (root / PATH).symlink_to("../../other")
    with pytest.raises(KernelError) as error:
        store(tmp_path).prepare(package, root)
    assert error.value.code == "eval_delivery_target_dirty"


def test_execute_rechecks_dirty_workspace_after_approval(tmp_path: Path) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    pending = deliveries.prepare(package, root)
    deliveries.decide(pending.delivery_id, decision(pending))
    (root / "user.tmp").write_text("preserve", encoding="utf-8")

    with pytest.raises(KernelError) as error:
        deliveries.execute(pending.delivery_id, root)

    assert error.value.code == "eval_delivery_target_dirty"
    assert (root / PATH).read_text(encoding="utf-8") == "value = 1\n"
    assert (root / "user.tmp").read_text(encoding="utf-8") == "preserve"
    assert deliveries.get(pending.delivery_id).status == "approved"


@pytest.mark.parametrize(
    ("fault_point", "expected"),
    [
        ("delivery.intent_persisted", "approved"),
        ("delivery.before_replace", "approved"),
        ("delivery.after_replace", "applied"),
        ("delivery.directories_synced", "applied"),
    ],
)
def test_crash_reconciliation_uses_persisted_inode_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point: str, expected: str
) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    pending = deliveries.prepare(package, root)
    deliveries.decide(pending.delivery_id, decision(pending))

    def crash(point: str) -> None:
        if point == fault_point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(delivery_module, "_fault", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        deliveries.execute(pending.delivery_id, root)
    assert deliveries.get(pending.delivery_id).status == "applying"

    monkeypatch.setattr(delivery_module, "_fault", lambda _: None)
    reopened = CodingEvalDeliveryStore(deliveries.root, git_executable())
    reconciled = reopened.reconcile(pending.delivery_id, root)
    assert reconciled.status == expected
    if expected == "approved":
        reconciled = reopened.execute(pending.delivery_id, root)
    assert reconciled.status == "applied"
    assert (root / PATH).read_text(encoding="utf-8") == "value = 2\n"
    assert not list((root / "src").glob("*.harnessix-*.tmp"))


@pytest.mark.parametrize(
    ("body", "expected"), [("third\n", "conflicted"), ("value = 2\n", "unknown")]
)
def test_reconcile_never_attributes_external_target_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    expected: str,
) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    pending = deliveries.prepare(package, root)
    deliveries.decide(pending.delivery_id, decision(pending))

    monkeypatch.setattr(
        delivery_module,
        "_fault",
        lambda point: (
            (_ for _ in ()).throw(RuntimeError("crash"))
            if point == "delivery.intent_persisted"
            else None
        ),
    )
    with pytest.raises(RuntimeError):
        deliveries.execute(pending.delivery_id, root)
    (root / PATH).write_text(body, encoding="utf-8")
    monkeypatch.setattr(delivery_module, "_fault", lambda _: None)

    result = deliveries.reconcile(pending.delivery_id, root)

    assert result.status == expected
    assert (root / PATH).read_text(encoding="utf-8") == body
    assert not list((root / "src").glob("*.harnessix-*.tmp"))


def test_lock_and_existing_delivery_id_fail_closed(tmp_path: Path) -> None:
    root, package = repository(tmp_path)
    deliveries = store(tmp_path)
    identifier = uuid4()
    pending = deliveries.prepare(package, root, delivery_id=identifier)
    marker = deliveries.root / str(identifier) / "state.json"
    original = marker.read_bytes()

    with pytest.raises(KernelError) as error:
        deliveries.prepare(package, root, delivery_id=identifier)
    assert error.value.code == "eval_delivery_prepare_failed"
    assert marker.read_bytes() == original

    descriptor = os.open(deliveries.root / str(identifier) / "owner.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(KernelError) as error:
            deliveries.get(pending.delivery_id)
        assert error.value.code == "eval_delivery_busy"
    finally:
        os.close(descriptor)


async def test_real_passed_eval_builds_package_and_detects_late_workspace_drift(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    result = await run(tmp_path, run_id, HistoricalFixProvider())

    package = await build_coding_eval_change_package(
        tmp_path,
        git_executable(),
        historical_coding_eval(TASK_ID),
        run_id,
    )

    assert package.run_id == run_id
    assert package.report_sha256 == result.state.report_sha256
    assert package.path == "src/harnessix/models/_chat_stream.py"
    assert "if part.id is not None:" in package.before.text
    assert 'if part.id not in (None, ""):' in package.after.text
    assert package.package_fingerprint == digest(
        package.model_dump(mode="json", exclude={"package_fingerprint"})
    )
    assert (
        await build_coding_eval_change_package(
            tmp_path, git_executable(), historical_coding_eval(TASK_ID), run_id
        )
        == package
    )

    definition = historical_coding_eval(TASK_ID)
    target = tmp_path / str(run_id) / "delivery-target"
    clone = await asyncio.create_subprocess_exec(
        str(git_executable()),
        "clone",
        "-q",
        "--no-local",
        str(ROOT),
        str(target),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    assert await clone.wait() == 0
    git(target, "checkout", "-q", "--detach", package.repository.source_revision)
    git(target, "remote", "set-url", "origin", package.repository.origin)
    deliveries = CodingEvalDeliveryStore(tmp_path / "deliveries", git_executable())
    pending = deliveries.prepare(package, target)
    deliveries.decide(pending.delivery_id, decision(pending))
    applied = deliveries.execute(pending.delivery_id, target)
    assert applied.status == "applied"
    evidence = await collect_git_evidence(
        target,
        git_executable(),
        baseline_revision=package.repository.source_revision,
        baseline_tree_sha256=package.repository.baseline_tree_sha256,
    )
    assert evidence.changed_paths == (package.path,)
    assert evidence.diff_sha256 == package.workspace_diff_sha256
    materialized = load_materialized_coding_eval(tmp_path, git_executable(), definition, run_id)
    checks = await run_historical_checks(
        definition,
        materialized,
        Path(sys.executable),
        "final",
        workspace=target,
    )
    assert all(check.passed for check in checks)

    path = result.workspace / package.path
    path.write_text(package.after.text + "# drift\n", encoding="utf-8")
    with pytest.raises(KernelError) as error:
        await build_coding_eval_change_package(
            tmp_path,
            git_executable(),
            historical_coding_eval(TASK_ID),
            run_id,
        )
    assert error.value.code == "eval_change_package_workspace_drift"
