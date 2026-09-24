"""Action恢复候选入口的Profile发现与前置校验回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_action_recovery_soak_candidate


def test_profile_discovery_rejects_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_action_recovery_soak_candidate, "_ARCHIVE", tmp_path)
    with pytest.raises(KernelError) as rejected:
        run_action_recovery_soak_candidate._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


def test_profile_discovery_rejects_ambiguous_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_action_recovery_soak_candidate, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / ("a" * 32)).mkdir(parents=True)
    (platform / ("b" * 32)).mkdir()
    with pytest.raises(KernelError) as rejected:
        run_action_recovery_soak_candidate._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


def test_profile_discovery_rejects_non_identity_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_action_recovery_soak_candidate, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / "not-a-profile-id").mkdir(parents=True)
    with pytest.raises(KernelError) as rejected:
        run_action_recovery_soak_candidate._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"
