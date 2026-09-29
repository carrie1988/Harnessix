"""不同版本验收拒绝同版本、来源漂移和错误阶段；不接触用户状态。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import yaml

from scripts import installed_product_upgrade_acceptance as upgrade
from scripts.installed_product_upgrade_acceptance import (
    AcceptanceFailure,
    check_upgrade_pair,
    read_wheel_identity,
    require_phase_threads,
)


def _wheel(tmp_path: Path, name: str, version: str, *, duplicate: bool = False) -> Path:
    path = tmp_path / f"{name}.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr("harnessix/__init__.py", b'"""wheel fixture"""\n')
        metadata = f"Name: harnessix\nVersion: {version}\n"
        if duplicate:
            metadata += f"Version: {version}\n"
        archive.writestr(f"harnessix-{version}.dist-info/METADATA", metadata)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_upgrade_identity_uses_actual_wheel_metadata_and_pinned_bytes(tmp_path: Path) -> None:
    old = _wheel(tmp_path, "old", "0.1.0")
    new = _wheel(tmp_path, "new", "1.0.0rc1")
    before = read_wheel_identity(old, _sha(old))
    after = read_wheel_identity(new, _sha(new))
    assert before.version == "0.1.0" and after.version == "1.0.0rc1"
    check_upgrade_pair(before, after)


def test_same_version_reinstall_cannot_be_reported_as_upgrade(tmp_path: Path) -> None:
    old = _wheel(tmp_path, "old", "0.1.0")
    new = _wheel(tmp_path, "new", "0.1.0")
    with pytest.raises(AcceptanceFailure, match="upgrade_versions_equal"):
        check_upgrade_pair(read_wheel_identity(old, _sha(old)), read_wheel_identity(new, _sha(new)))


@pytest.mark.parametrize("version", ["1.0.0", "0.9.5rc1", "not-a-version"])
def test_unregistered_candidate_version_is_refused(tmp_path: Path, version: str) -> None:
    old = _wheel(tmp_path, "old", "0.1.0")
    new = _wheel(tmp_path, "new", version)
    with pytest.raises(AcceptanceFailure, match="upgrade_version_scope_invalid"):
        check_upgrade_pair(read_wheel_identity(old, _sha(old)), read_wheel_identity(new, _sha(new)))


@pytest.mark.parametrize("digest", ["0" * 64, "missing", "A" * 64])
def test_upgrade_refuses_wrong_digest_before_package_install(tmp_path: Path, digest: str) -> None:
    wheel = _wheel(tmp_path, "candidate", "1.0.0rc1")
    with pytest.raises(AcceptanceFailure, match="upgrade_wheel_identity_invalid"):
        read_wheel_identity(wheel, digest)


def test_upgrade_refuses_ambiguous_metadata(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, "duplicate", "1.0.0rc1", duplicate=True)
    with pytest.raises(AcceptanceFailure, match="upgrade_wheel_metadata_invalid"):
        read_wheel_identity(wheel, _sha(wheel))


@pytest.mark.parametrize("name", ["another-package", "../harnessix"])
def test_upgrade_refuses_unrelated_distribution(tmp_path: Path, name: str) -> None:
    wheel = tmp_path / "invalid.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "harnessix-1.0.0rc1.dist-info/METADATA", f"Name: {name}\nVersion: 1.0.0rc1\n"
        )
    with pytest.raises(AcceptanceFailure, match="upgrade_wheel_metadata_invalid"):
        read_wheel_identity(wheel, _sha(wheel))


@pytest.mark.parametrize("observed", [{"A", "B"}, set(), {"C"}])
def test_rollback_requires_exact_original_threads_not_just_nonempty_state(
    observed: set[str],
) -> None:
    with pytest.raises(AcceptanceFailure, match="upgrade_thread_state_invalid"):
        require_phase_threads(observed, {"A"})
    require_phase_threads({"A"}, {"A"})


def test_upgrade_workflow_reuses_one_candidate_and_keeps_private_state_unpublished() -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load(
        (root / ".github/workflows/installed-product-acceptance.yml").read_text()
    )
    consumer = workflow["jobs"]["installed-product"]
    steps = consumer["steps"]
    transition = next(step for step in steps if step.get("id") == "version-transition")
    assert "installed_product_upgrade_acceptance.py" in transition["run"]
    assert "--wheel-sha256" in transition["run"]
    assert "--baseline-sha256" in transition["run"]
    assert "--require-hashes" in transition["run"] and "--no-deps" in transition["run"]
    assert (
        "--baseline-source-revision a4f7f33449bb897d84fe3a8e8262307943233fb4" in transition["run"]
    )
    assert "5a6e144487dd79679f609b709743db176ee28bdca077084ac8901efdc9f2fe30" in transition["run"]
    upload = next(step for step in steps if step.get("id") == "upgrade-evidence")
    paths = upload["with"]["path"].splitlines()
    assert paths and all("/case/" not in path and "*" not in path for path in paths)
    assert any(path.endswith("/result.json") for path in paths)
    assert all(not path.endswith((".db", "key.v1")) for path in paths)


def test_phase_timeout_stops_without_retry_or_private_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []

    def timeout(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 180
        raise subprocess.TimeoutExpired(command, 180, output=b"private phase data")

    monkeypatch.setattr(upgrade.subprocess, "run", timeout)
    arguments = Namespace(
        environment_root=tmp_path,
        source_root=tmp_path / "source",
        source_revision="a" * 40,
        uv=tmp_path / "uv",
    )
    with pytest.raises(AcceptanceFailure, match="^upgrade_phase_failed$"):
        upgrade.run_phase(arguments, tmp_path / "wheel.whl", "b" * 64, "baseline")
    assert len(commands) == 1
    assert commands[0][:2] == (sys.executable, "-I")


@pytest.mark.parametrize(
    "changed", [None, "installed_product_acceptance.py", "installed_product_upgrade_acceptance.py"]
)
def test_actual_acceptance_scripts_must_match_committed_revision_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    prefix = tmp_path / "run/venv"
    source = tmp_path / "source"
    revision = "a" * 40
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(upgrade._helpers, "check_environment", lambda *args, **kwargs: None)
    monkeypatch.setattr(upgrade._helpers, "check_package_members", lambda *args: 1)
    monkeypatch.setattr(upgrade._helpers, "version", lambda _name: "1.0.0rc1")
    monkeypatch.setattr(
        upgrade._helpers.harnessix, "__file__", str(prefix / "harnessix/__init__.py")
    )

    def committed(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=revision)
        filename = command[-1].split(":scripts/")[1]
        content = Path(upgrade.__file__).with_name(filename).read_bytes()
        return SimpleNamespace(stdout=content if filename != changed else content + b"\n")

    monkeypatch.setattr(upgrade.subprocess, "run", committed)
    arguments = Namespace(
        environment_root=prefix.parent, source_root=source, source_revision=revision
    )
    identity = upgrade.WheelIdentity("1.0.0rc1", "b" * 64)
    if changed is None:
        assert upgrade._environment(arguments, tmp_path / "wheel.whl", identity) == 1
    else:
        with pytest.raises(AcceptanceFailure, match="^upgrade_script_revision_mismatch$"):
            upgrade._environment(arguments, tmp_path / "wheel.whl", identity)


def test_controller_cancellation_has_fixed_exit_and_no_success_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upgrade",
            "--environment-root",
            str(tmp_path),
            "--source-root",
            str(tmp_path / "source"),
            "--source-revision",
            "a" * 40,
            "--wheel",
            str(tmp_path / "new.whl"),
            "--wheel-sha256",
            "b" * 64,
            "--baseline-wheel",
            str(tmp_path / "old.whl"),
            "--baseline-sha256",
            "c" * 64,
            "--uv",
            str(tmp_path / "uv"),
        ],
    )

    def cancel(_arguments):
        raise KeyboardInterrupt

    monkeypatch.setattr(upgrade, "accept_upgrade", cancel)
    assert upgrade.main() == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert '"code": "upgrade_cancelled"' in captured.err
    assert not (tmp_path / "result.json").exists()
