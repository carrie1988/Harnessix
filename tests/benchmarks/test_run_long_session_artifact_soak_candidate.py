"""长会话与Artifact候选入口的Profile发现前置校验回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_artifact_growth_soak_candidate, run_long_session_soak_candidate


@pytest.mark.parametrize(
    "module",
    [run_long_session_soak_candidate, run_artifact_growth_soak_candidate],
)
def test_profile_discovery_rejects_missing_directory(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_ARCHIVE", tmp_path)
    with pytest.raises(KernelError) as rejected:
        module._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


@pytest.mark.parametrize(
    "module",
    [run_long_session_soak_candidate, run_artifact_growth_soak_candidate],
)
def test_profile_discovery_rejects_ambiguous_directory(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / ("a" * 32)).mkdir(parents=True)
    (platform / ("b" * 32)).mkdir()
    with pytest.raises(KernelError) as rejected:
        module._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


@pytest.mark.parametrize(
    "module",
    [run_long_session_soak_candidate, run_artifact_growth_soak_candidate],
)
def test_profile_discovery_rejects_non_identity_name(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / "not-a-profile-id").mkdir(parents=True)
    with pytest.raises(KernelError) as rejected:
        module._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"
