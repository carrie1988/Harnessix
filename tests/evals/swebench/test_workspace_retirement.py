from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

import harnessix.evals.swebench.workspace_retirement as retirement
import harnessix.evals.swebench.workspace_retirement_fs as retirement_fs
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatusV18
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    build_swebench_case_execution_state,
)
from harnessix.evals.swebench.execution_io import write_case_execution_state
from harnessix.evals.swebench.run_config import SweBenchRecyclingAgentRunConfig, SweBenchRunConfig
from tests.evals.swebench.test_run_config import _config, build_agent_test_plan

KEY = "local-retirement-test-credential"
SOURCE_TARGETS = ("native", "state_file", "workspace", "case_root", "seal", "marker", "root")
PATCH = (
    "diff --git a/example.py b/example.py\n"
    "--- a/example.py\n+++ b/example.py\n"
    "@@ -1 +1 @@\n-old\n+new 中文\n"
)


def _private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _file(path: Path, body: bytes = b"original-evidence", mode: int = 0o600) -> None:
    path.write_bytes(body)
    path.chmod(mode)


def _image(path: Path) -> tuple[int, int, int, bytes]:
    info = path.lstat()
    return info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), path.read_bytes()


@dataclass
class Case:
    plan: SweBenchPreRegistrationPlan
    config: SweBenchRunConfig
    root: Path
    case_root: Path
    workspace: Path
    native: Path
    state: SweBenchCaseExecutionState
    patch: str
    preserved: dict[Path, tuple[int, int, int, bytes]]

    @property
    def seal(self) -> Path:
        return self.case_root / retirement.SEALED_WORKSPACE_FILE

    @property
    def marker(self) -> Path:
        return self.workspace / retirement.RETIRED_WORKSPACE_MARKER

    @property
    def state_file(self) -> Path:
        return self.case_root / "case-state.json"

    def retire(self, **kwargs):
        return retirement.seal_and_retire_workspace(
            self.plan,
            self.config,
            0,
            self.state,
            self.patch,
            api_key=KEY,
            **kwargs,
        )

    def read(self, *, key: str = KEY):
        return retirement.read_retired_patch(self.plan, self.config, 0, api_key=key)

    def assert_preserved(self) -> None:
        assert {path: _image(path) for path in self.preserved} == self.preserved


def _case(tmp_path: Path, *, patch: str = PATCH, provider_failure: bool = False) -> Case:
    plan = build_agent_test_plan()
    config = _config(tmp_path, plan=plan, isolate_provider_failures=provider_failure)
    config = SweBenchRecyclingAgentRunConfig.model_validate(
        {
            **config.model_dump(),
            "spec_version": "harnessix.swebench-agent-run-config/v9",
        }
    )
    root = Path(config.work_root)
    case_root = root / "cases" / "case-000"
    workspace = root / "workspaces" / plan.cases[0].instance_id
    for directory in (
        root,
        root / "cases",
        case_root,
        case_root / "run",
        root / "workspaces",
        workspace,
    ):
        _private(directory)
    _private(workspace / ".git")
    _file(workspace / ".git" / "config", b"fixture-git-metadata", 0o644)
    _private(workspace / "build")
    _file(workspace / "build" / "output", b"discard-me", 0o644)
    _file(workspace / "example.py", b"new", 0o644)
    native = case_root / "run" / "session.sqlite"
    with sqlite3.connect(native) as database:
        database.execute("CREATE TABLE native_evidence (value TEXT NOT NULL)")
        database.execute("INSERT INTO native_evidence VALUES ('original-closed-turn')")
    native.chmod(0o600)
    state = build_swebench_case_execution_state(
        plan,
        plan.cases[0].instance_id,
        uuid4(),
        status="failed" if patch == "" or provider_failure else "completed",
        thread_id=uuid4(),
        turn_id=uuid4(),
        turn_status=TurnStatusV18.FAILED if provider_failure else TurnStatusV18.COMPLETED,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        input_tokens=0 if provider_failure else 10,
        output_tokens=0 if provider_failure else 20,
        error_code=(
            "swebench_provider_transport_isolated"
            if provider_failure
            else "swebench_patch_missing"
            if patch == ""
            else None
        ),
    )
    write_case_execution_state(case_root / "case-state.json", plan, state)
    paths = [native, case_root / "case-state.json"]
    for directory in (
        case_root / "run" / "artifacts",
        root / "cases" / "case-001",
        root / "workspaces" / plan.cases[1].instance_id,
        Path(config.cache_root),
        Path(config.source_root),
    ):
        _private(directory)
        path = directory / "keep"
        _file(path)
        paths.append(path)
    for name in ("fees.json", "session.sqlite-wal", "session.sqlite-shm"):
        path = case_root / "run" / name
        _file(path)
        paths.append(path)
    return Case(
        plan,
        config,
        root,
        case_root,
        workspace,
        native,
        state,
        patch,
        {path: _image(path) for path in paths},
    )


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return _case(tmp_path)


@pytest.mark.parametrize(
    "patch,provider_failure",
    [
        (PATCH, False),
        ("", False),
        ("", True),
        ("x" * (8 * 1024 * 1024), False),
    ],
)
def test_signed_roundtrip_preserves_native_evidence(
    tmp_path: Path, patch: str, provider_failure: bool
) -> None:
    case = _case(tmp_path, patch=patch, provider_failure=provider_failure)
    root_identity = case.workspace.stat().st_ino
    manifest = case.retire()
    assert case.read() == patch
    sealed_images = _image(case.seal), _image(case.marker)
    assert case.retire() == manifest
    assert sealed_images == (_image(case.seal), _image(case.marker))
    assert {path.name for path in case.case_root.iterdir()} == {
        "case-state.json",
        "run",
        "sealed-workspace.json",
    }
    assert manifest.patch_bytes == len(patch.encode())
    assert manifest.patch_sha256 == hashlib.sha256(patch.encode()).hexdigest()
    assert case.workspace.stat().st_ino == root_identity
    assert list(case.workspace.iterdir()) == [case.marker]
    assert stat.S_IMODE(case.seal.stat().st_mode) == 0o600
    assert stat.S_IMODE(case.marker.stat().st_mode) == 0o600
    assert KEY.encode() not in case.seal.read_bytes() + case.marker.read_bytes()
    case.assert_preserved()


@pytest.mark.parametrize("missing", ["native", "root"])
def test_no_seal_returns_none_without_native_or_work_root(case: Case, missing: str) -> None:
    if missing == "root":
        shutil.rmtree(case.root)
    else:
        case.native.unlink()
    assert case.read() is None


class InjectedFault(RuntimeError):
    pass


@pytest.mark.parametrize(
    "status", ["ready", "running", "cancelled", "failed", "interrupted", "unexecuted_quality"]
)
def test_only_executed_closed_terminal_states_can_be_sealed(case: Case, status: str) -> None:
    kwargs: dict = {}
    if status == "running":
        kwargs = {"thread_id": uuid4()}
    elif status in {"failed", "unexecuted_quality", "interrupted"}:
        kwargs = {"error_code": "swebench_patch_missing" if status != "failed" else "other_failure"}
        if status == "interrupted":
            kwargs.update(thread_id=uuid4(), turn_id=uuid4(), turn_status=TurnStatusV18.INTERRUPTED)
    state = build_swebench_case_execution_state(
        case.plan,
        case.plan.cases[0].instance_id,
        case.state.run_id,
        status="failed" if status in {"interrupted", "unexecuted_quality"} else status,
        **kwargs,
    )
    write_case_execution_state(case.state_file, case.plan, state)
    with pytest.raises(KernelError):
        retirement.seal_and_retire_workspace(case.plan, case.config, 0, state, "", api_key=KEY)
    assert not case.seal.exists()
    assert not case.marker.exists()
    assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        b"patch",
        17,
        "\ud800",
        PATCH + "different",
        "x" * (8 * 1024 * 1024 + 1),
        "中" * (3 * 1024 * 1024),
    ],
)
def test_patch_type_utf8_digest_and_byte_limit_rejected(case: Case, invalid) -> None:
    with pytest.raises(KernelError) as failure:
        retirement.seal_and_retire_workspace(
            case.plan, case.config, 0, case.state, invalid, api_key=KEY
        )
    assert KEY not in str(failure.value)
    assert PATCH not in str(failure.value)
    assert not case.seal.exists()
    assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize("index", [-1, 25, True, "0", None])
def test_invalid_index_rejected(case: Case, index) -> None:
    with pytest.raises(KernelError):
        retirement.read_retired_patch(case.plan, case.config, index, api_key=KEY)


@pytest.mark.parametrize("key", ["wrong", "", None, b"credential", "\ud800"])
def test_wrong_or_invalid_key_never_returns_patch(case: Case, key) -> None:
    case.retire()
    with pytest.raises(KernelError) as failure:
        case.read(key=key)
    assert KEY not in str(failure.value)
    assert PATCH not in str(failure.value)


def test_other_plan_config_case_and_root_replays_rejected(case: Case, tmp_path: Path) -> None:
    case.retire()
    other_plan = case.plan.model_copy(update={"model_name": "different/model"})
    other_config = case.config.model_copy(update={"fee_stop_amount": "81"})
    for plan, config, index in (
        (other_plan, case.config, 0),
        (case.plan, other_config, 0),
        (case.plan, case.config, 1),
    ):
        if index == 1:
            _file(
                case.root / "cases" / "case-001" / retirement.SEALED_WORKSPACE_FILE,
                case.seal.read_bytes(),
            )
        with pytest.raises(KernelError):
            retirement.read_retired_patch(plan, config, index, api_key=KEY)
    copy_root = tmp_path / "copied-run"
    shutil.copytree(case.root, copy_root)
    copy_root.chmod(0o700)
    with pytest.raises(KernelError):
        retirement.read_retired_patch(
            case.plan, case.config.model_copy(update={"work_root": str(copy_root)}), 0, api_key=KEY
        )


def test_orphan_marker_fails_closed_for_reader_and_retire(case: Case) -> None:
    _file(case.marker, b"forged marker")
    with pytest.raises(KernelError):
        case.read()
    with pytest.raises(KernelError):
        case.retire()
    assert not case.seal.exists()
    assert (case.workspace / "example.py").exists()


def test_child_links_to_all_boundaries_are_unlinked_without_following(case: Case) -> None:
    for index, boundary in enumerate(
        (
            Path(case.config.source_root),
            Path(case.config.cache_root),
            case.case_root,
            case.root / "workspaces" / case.plan.cases[1].instance_id,
            case.native,
            case.workspace / "nonexistent",
            case.workspace,
        )
    ):
        (case.workspace / f"link-{index}").symlink_to(boundary)
    case.retire()
    assert list(case.workspace.iterdir()) == [case.marker]
    case.assert_preserved()


@pytest.mark.parametrize(
    "kind",
    [
        "hardlink",
        "fifo",
        "writable_file",
        "writable_directory",
        "foreign_device",
        "foreign_owner",
    ],
)
def test_unsafe_tree_preflight_refuses_deletion(
    case: Case, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = case.workspace / "z-unsafe"
    if kind == "hardlink":
        os.link(case.native, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "writable_file":
        _file(path, mode=0o666)
    elif kind == "writable_directory":
        _private(path)
        path.chmod(0o777)
    else:
        _file(path)
        real_stat = os.stat

        def foreign_stat(name, *args, **kwargs):
            info = real_stat(name, *args, **kwargs)
            if name == path.name and kwargs.get("dir_fd") is not None:
                values = list(info)
                values[2 if kind == "foreign_device" else 4] += 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(retirement_fs.os, "stat", foreign_stat)
    with pytest.raises(KernelError):
        case.retire()
    assert (case.workspace / ".git" / "config").exists()
    assert (case.workspace / "build" / "output").exists()
    assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize(
    "stage,mutation",
    [
        ("after_seal", "native"),
        ("after_marker", "native"),
        ("after_seal", "seal"),
        ("after_marker", "seal"),
        ("after_marker", "workspace"),
    ],
)
def test_hook_drift_stops_deletion(case: Case, stage: str, mutation: str) -> None:
    backup = case.workspace.with_name(case.workspace.name + "-backup")

    def change(point: str) -> None:
        if point != stage:
            return
        if mutation == "workspace":
            case.workspace.rename(backup)
            _private(case.workspace)
        else:
            with getattr(case, mutation).open("ab") as output:
                output.write(b"changed")

    with pytest.raises(KernelError):
        case.retire(fault=change)
    workspace = backup if backup.exists() else case.workspace
    assert (workspace / "example.py").exists()


def test_moved_nested_directory_stops_cleanup(case: Case, tmp_path: Path) -> None:
    nested = case.workspace / ".git"
    _file(nested / "second", b"must-survive", 0o644)
    outside = tmp_path / "moved-git"

    def move(point: str) -> None:
        if point == "during_cleanup" and not outside.exists():
            nested.rename(outside)
            _private(nested)

    with pytest.raises(KernelError):
        case.retire(fault=move)
    assert (outside / "second").read_bytes() == b"must-survive"
    assert (case.workspace / "example.py").exists()


def test_state_argument_must_equal_formal_original_state(case: Case) -> None:
    other = case.state.model_copy(update={"thread_id": uuid4()})
    for state in (other, None):
        with pytest.raises(KernelError):
            retirement.seal_and_retire_workspace(
                case.plan, case.config, 0, state, PATCH, api_key=KEY
            )
    assert not case.seal.exists()


def test_state_from_different_plan_case_cannot_be_sealed(case: Case) -> None:
    state = build_swebench_case_execution_state(
        case.plan,
        case.plan.cases[1].instance_id,
        uuid4(),
        status="completed",
        thread_id=uuid4(),
        turn_id=uuid4(),
        turn_status=TurnStatusV18.COMPLETED,
        patch_sha256=hashlib.sha256(PATCH.encode()).hexdigest(),
        input_tokens=1,
    )
    write_case_execution_state(case.state_file, case.plan, state)
    with pytest.raises(KernelError):
        retirement.seal_and_retire_workspace(case.plan, case.config, 0, state, PATCH, api_key=KEY)
    assert not case.seal.exists()


@pytest.mark.parametrize(
    "mutation,target",
    [
        (mutation, target)
        for mutation, targets in (
            ("replacement", ["native", "state_file", "workspace", "case_root", "run_root"]),
            ("bytes", ["native", "state_file", "seal", "marker"]),
            ("permission", SOURCE_TARGETS),
            ("symlink", SOURCE_TARGETS),
            ("hardlink", ["native", "state_file", "seal", "marker"]),
            ("fifo", ["native", "state_file"]),
        )
        for target in targets
    ],
)
def test_evidence_mutations_fail_closed(
    case: Case, tmp_path: Path, mutation: str, target: str
) -> None:
    if mutation != "fifo":
        case.retire()
    path = case.case_root / "run" if target == "run_root" else getattr(case, target)
    if mutation in {"replacement", "symlink"}:
        original = path.with_name(path.name + "-original")
        path.rename(original)
        if mutation == "symlink":
            path.symlink_to(original, target_is_directory=original.is_dir())
        elif original.is_dir():
            shutil.copytree(original, path)
            path.chmod(0o700)
        else:
            _file(path, original.read_bytes())
    elif mutation == "bytes":
        with path.open("ab") as output:
            output.write(b"drift")
    elif mutation == "permission":
        path.chmod(0o755 if path.is_dir() else 0o644)
    elif mutation == "hardlink":
        os.link(path, tmp_path / "hardlink")
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    if mutation != "fifo":
        with pytest.raises(KernelError):
            case.read()
    with pytest.raises(KernelError):
        case.retire()
    if mutation == "fifo":
        assert not case.seal.exists()
        assert (case.workspace / "example.py").exists()


@pytest.mark.parametrize("resign", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("patch", "sensitive-forged-patch"),
        ("patch_bytes", 0),
        ("instance_id", "owner__repo-999"),
        ("session_sha256", "0" * 64),
        ("workspace_path", "/outside"),
        ("patch_sha256", "0" * 64),
        ("case_index", 1),
        ("thread_id", str(uuid4())),
        ("turn_id", str(uuid4())),
        ("run_id", str(uuid4())),
        ("state_sha256", "0" * 64),
        ("base_commit", "a" * 40),
        ("directory_identities", {}),
        ("patch", None),
    ],
)
def test_signed_and_unsigned_tampering_is_rejected(
    case: Case, field: str, value, resign: bool
) -> None:
    case.retire()
    payload = json.loads(case.seal.read_bytes())
    payload[field] = value
    if resign:
        unsigned = {key: item for key, item in payload.items() if key != "hmac_sha256"}
        payload["hmac_sha256"] = retirement._signature(unsigned, retirement._key(KEY))
    _file(case.seal, retirement._canonical(payload) + b"\n")
    with pytest.raises(KernelError) as failure:
        case.read()
    assert "sensitive-forged-patch" not in str(failure.value)
    with pytest.raises(KernelError):
        case.retire()
    case.assert_preserved()


@pytest.mark.parametrize(
    "kind,point",
    [
        (kind, point)
        for kind, points in (
            ("fault", ["after_seal", "after_marker", "during_cleanup"]),
            ("cancel", ["before", "after_seal", "after_marker", "during_cleanup"]),
        )
        for point in points
    ],
)
def test_interruption_recovers_without_git_or_duplicate_seals(
    case: Case, kind: str, point: str
) -> None:
    inode = case.workspace.stat().st_ino
    before = set(case.workspace.rglob("*"))
    token = CancelToken()
    if point == "before":
        token.cancel()

    def interrupt(stage: str) -> None:
        if stage == point:
            if kind == "cancel":
                token.cancel()
            else:
                raise InjectedFault(stage)

    with pytest.raises(TurnCancelled if kind == "cancel" else InjectedFault):
        case.retire(cancel=token, fault=interrupt)
    seal_image = _image(case.seal) if case.seal.exists() else None
    if point == "before":
        assert not case.seal.exists() and not case.marker.exists()
    else:
        assert case.read() == PATCH
    remaining = set(case.workspace.rglob("*")) - {case.marker}
    if point != "during_cleanup":
        assert remaining == before
    else:
        assert len(remaining) < len(before)
    assert case.marker.exists() == (point in {"after_marker", "during_cleanup"})
    case.assert_preserved()
    shutil.rmtree(case.workspace / ".git", ignore_errors=True)
    assert case.retire().patch == PATCH
    if seal_image is not None:
        assert _image(case.seal) == seal_image
    assert case.workspace.stat().st_ino == inode
    assert list(case.workspace.iterdir()) == [case.marker]
    case.assert_preserved()
