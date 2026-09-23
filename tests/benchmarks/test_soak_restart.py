"""完整产品重启Runner的真实跨进程与失败Attempt回归。"""

from __future__ import annotations

from pathlib import Path
from subprocess import check_output

import pytest

from harnessix.agent.errors import KernelError
from scripts.run_restart_soak_release import main as release_main
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_restart import run_product_restart
from scripts.soak_restart_proof import RESTART_PROOF_FILENAME, SoakRestartProof


def _revision() -> str:
    return check_output(("git", "rev-parse", "HEAD"), text=True).strip()


async def test_product_restart_uses_real_product_and_publishes_unverified_small_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    run_directory, manifest = await run_product_restart(
        root,
        code_revision=_revision(),
        thread_count=3,
        warmup_count=1,
        measured_restarts=3,
        timeout_seconds=30,
    )

    assert manifest.status == "unverified"
    assert manifest.sample_counts == {"product_startup": 3, "rss_peak": 1}
    assert manifest.provider.request_count == 0
    assert manifest.fault_counts.eof == 1
    assert read_published_run(run_directory)[0] == manifest
    assert read_attempt(root / "attempts" / manifest.run_id)[1].outcome == "committed"
    proof = SoakRestartProof.model_validate_json(
        (run_directory / RESTART_PROOF_FILENAME).read_bytes()
    )
    assert [cycle.owner_generation for cycle in proof.cycles] == [1, 2, 3, 4, 5]
    assert [cycle.phase for cycle in proof.cycles] == [
        "warmup",
        "crash",
        "measure",
        "measure",
        "measure",
    ]
    assert proof.hard_exit_ack and proof.hard_exit_eof
    assert all(cycle.recovery_scan.scanned_routes == 0 for cycle in proof.cycles)


async def test_product_restart_failure_keeps_failed_attempt_without_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_after_start(*_: object) -> set[object]:
        raise KernelError("soak_restart_thread_invalid", "固定故障夹具")

    monkeypatch.setattr("scripts.soak_restart._create_threads", fail_after_start)
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_product_restart(
            root,
            code_revision=_revision(),
            thread_count=2,
            warmup_count=1,
            measured_restarts=3,
            timeout_seconds=30,
        )
    assert error.value.code == "soak_restart_thread_invalid"
    attempts = list((root / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (root / attempts[0].name).exists()


async def test_product_restart_startup_failure_has_stable_code_and_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_invalid_config(path: Path) -> None:
        path.write_bytes(b"{}\n")
        path.chmod(0o600)

    monkeypatch.setattr("scripts.soak_restart._write_offline_config", write_invalid_config)
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_product_restart(
            root,
            code_revision=_revision(),
            thread_count=2,
            warmup_count=1,
            measured_restarts=3,
            timeout_seconds=30,
        )
    assert error.value.code == "soak_restart_startup_failed"
    attempts = list((root / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (root / attempts[0].name).exists()


@pytest.mark.parametrize(
    ("thread_count", "warmup_count", "measured_restarts", "timeout_seconds"),
    [(0, 1, 3, 30), (2, 0, 3, 30), (2, 1, 2, 30), (2, 1, 3, 19)],
)
async def test_product_restart_rejects_invalid_load_before_attempt(
    tmp_path: Path,
    thread_count: int,
    warmup_count: int,
    measured_restarts: int,
    timeout_seconds: float,
) -> None:
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_product_restart(
            root,
            code_revision=_revision(),
            thread_count=thread_count,
            warmup_count=warmup_count,
            measured_restarts=measured_restarts,
            timeout_seconds=timeout_seconds,
        )
    assert error.value.code == "soak_load_invalid"
    assert not root.exists()


def test_restart_release_cli_redacts_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_release(_: Path) -> dict[str, str]:
        raise KernelError("soak_revision_invalid", "不得输出的本地路径和凭据")

    monkeypatch.setattr("scripts.run_restart_soak_release.run_release", fail_release)
    assert release_main(["--evidence-root", str(tmp_path / "evidence")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "soak_revision_invalid" in captured.err
    assert "不得输出" not in captured.err
