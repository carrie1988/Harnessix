"""匹配备份回退的调用顺序；格式替身不是实际安装或数据库验真。"""

from argparse import Namespace
from uuid import UUID

import pytest

from scripts import installed_product_upgrade_acceptance as subject


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    old, new = tmp_path / "old.whl", tmp_path / "new.whl"
    arguments = Namespace(
        environment_root=tmp_path,
        source_root=tmp_path / "source",
        source_revision="a" * 40,
        uv=tmp_path / "uv",
        wheel=new,
        wheel_sha256="b" * 64,
        baseline_wheel=old,
        baseline_sha256=subject._BASELINE_SHA256,
        baseline_source_revision=subject._BASELINE_REVISION,
    )
    state = {"schema": "1", "events": [], "restore_calls": [], "restores": []}
    case = subject._case(tmp_path)

    def identity(path, digest):
        return subject.WheelIdentity("0.1.0" if path == old else "1.0.0rc1", digest)

    def install(args, path, digest, phase):
        state["events"].append(("install", phase))

    def phase(args, path, digest, name, before=(), backup_id="", restore_id=""):
        state["events"].append(("phase", name))
        if name == "baseline":
            key = case.state / "session-auth/key.v1"
            key.parent.mkdir(parents=True)
            key.write_bytes(b"isolated-test-key")
            case.workspace.mkdir()
            (case.workspace / "preserved.txt").write_bytes(b"unchanged workspace\n")
            threads = ["original"]
        elif name == "upgraded":
            state["schema"] = "2"
            threads = ["original", "upgraded"]
        elif name == "restore":
            # 当前Runtime验收会再次前向升代；原恢复ID重用不会撤销其新状态。
            state["schema"] = "2"
            state["restores"].append(restore_id)
            threads = ["original", "restored-current"]
        else:
            assert name == "rollback"
            if state["schema"] != "1":
                raise subject.AcceptanceFailure("delivery_store_version")
            assert before == ("original",)
            threads = ["original", "rollback-new"]
        return {
            "phase": name,
            "version": "0.1.0" if path == old else "1.0.0rc1",
            "threads": threads,
            "backup_id": "matching-backup",
        }

    def cli(args):
        assert args[:2] == ("state", "restore")
        assert args[-2:] == ("--confirm-backup", "matching-backup")
        state["events"].append(("matching-restore", "candidate-cli-only"))
        restore_id = args[args.index("--restore-id") + 1]
        assert restore_id not in state["restores"]
        state["restore_calls"].append(args)
        state["schema"] = "1"
        return {"status": "restored", "retained_previous_state": True}

    monkeypatch.setattr(subject, "read_wheel_identity", identity)
    monkeypatch.setattr(subject, "_environment", lambda *args: 552)
    monkeypatch.setattr(subject, "_install", install)
    monkeypatch.setattr(subject, "run_phase", phase)
    monkeypatch.setattr(subject._helpers, "cli", cli)
    return arguments, state, cli


def test_rollback_restores_matching_backup_after_last_candidate_runtime(scenario):
    arguments, state, _ = scenario
    result = subject.accept_upgrade(arguments)
    assert state["events"] == [
        ("install", "baseline"),
        ("phase", "baseline"),
        ("install", "candidate"),
        ("phase", "upgraded"),
        ("phase", "restore"),
        ("matching-restore", "candidate-cli-only"),
        ("install", "rollback"),
        ("phase", "rollback"),
    ]
    assert len(state["restore_calls"]) == 1
    assert result["rollback_matching_backup_restored_before_version_switch"] is True
    assert result["candidate_runtime_reopened_after_matching_restore"] is False
    assert result["rollback_reads_original_backup_threads"] is True


@pytest.mark.parametrize(
    "restored",
    [
        {"status": "failed", "retained_previous_state": True},
        {"status": "restored", "retained_previous_state": False},
    ],
)
def test_matching_restore_refusal_never_installs_old_binary(scenario, monkeypatch, restored):
    arguments, state, original = scenario

    def invalid(args):
        original(args)
        return restored

    monkeypatch.setattr(subject._helpers, "cli", invalid)
    with pytest.raises(subject.AcceptanceFailure, match="^installed_restore_invalid$"):
        subject.accept_upgrade(arguments)
    assert ("install", "rollback") not in state["events"]
    assert ("phase", "rollback") not in state["events"]


def test_matching_restore_preserves_original_exception_before_version_switch(scenario, monkeypatch):
    arguments, state, _ = scenario
    original = RuntimeError("private fixture failure")

    def failed(_):
        raise original

    monkeypatch.setattr(subject._helpers, "cli", failed)
    with pytest.raises(RuntimeError) as caught:
        subject.accept_upgrade(arguments)
    assert caught.value is original
    assert ("install", "rollback") not in state["events"]


def test_matching_restore_uses_new_identity_not_prior_idempotent_restore(scenario, monkeypatch):
    arguments, state, _ = scenario
    monkeypatch.setattr(subject, "uuid4", lambda: UUID(int=1))
    with pytest.raises(subject.AcceptanceFailure, match="^upgrade_restore_identity_reused$"):
        subject.accept_upgrade(arguments)
    assert state["restore_calls"] == []
    assert ("install", "rollback") not in state["events"]


def test_changed_key_after_matching_restore_never_switches_version(scenario, monkeypatch):
    arguments, state, original = scenario

    def changed(args):
        result = original(args)
        (subject._case(arguments.environment_root).state / "session-auth/key.v1").write_bytes(
            b"different-isolated-test-key"
        )
        return result

    monkeypatch.setattr(subject._helpers, "cli", changed)
    with pytest.raises(subject.AcceptanceFailure, match="^installed_restore_identity_invalid$"):
        subject.accept_upgrade(arguments)
    assert ("install", "rollback") not in state["events"]
