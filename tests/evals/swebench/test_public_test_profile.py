from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.public_test_profile import (
    PUBLIC_TEST_PROFILE_ID,
    discover_swebench_case_process_profiles,
)
from tests.evals.swebench.test_predictions import build_test_plan


def _docker(tmp_path: Path) -> Path:
    engine = tmp_path / "docker"
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o700)
    return engine


def _row(instance_id: str, image_id: str, *, platform: str = "x86_64") -> dict[str, str]:
    return {
        "Repository": f"swebench/sweb.eval.{platform}_{instance_id}",
        "Tag": "latest",
        "ID": image_id,
    }


def _runner(
    rows: Sequence[dict[str, str]],
    calls: list[tuple[tuple[str, ...], float]] | None = None,
):
    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append((tuple(argv), timeout))
        stdout = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def test_discovery_freezes_one_complete_profile_per_case_from_public_image_list(
    tmp_path: Path,
) -> None:
    plan = build_test_plan()
    engine = _docker(tmp_path)
    rows = [
        _row(case.instance_id, f"sha256:{index + 1:064x}")
        for index, case in enumerate(reversed(plan.cases))
    ]
    calls: list[tuple[tuple[str, ...], float]] = []

    bindings = discover_swebench_case_process_profiles(
        plan,
        engine,
        runner=_runner(rows, calls),
    )

    assert calls == [
        (
            (
                str(engine),
                "image",
                "ls",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            30.0,
        )
    ]
    assert tuple(binding.instance_id for binding in bindings) == tuple(
        sorted(case.instance_id for case in plan.cases)
    )
    for binding in bindings:
        profile = binding.profile
        assert profile.profile_id == PUBLIC_TEST_PROFILE_ID
        assert profile.image.startswith("sha256:") and len(profile.image) == 71
        assert profile.program == "/usr/bin/env"
        assert profile.arguments == (
            "HOME=/tmp",
            "TMPDIR=/tmp",
            "XDG_CACHE_HOME=/tmp",
            "MPLCONFIGDIR=/tmp",
            "HYPOTHESIS_STORAGE_DIRECTORY=/tmp",
            "PYTHONDONTWRITEBYTECODE=1",
            "/opt/miniconda3/envs/testbed/bin/python",
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--disable-warnings",
            "--tb=short",
            "--maxfail=1",
        )
        assert profile.selector_policy == "bounded_test_selector"
        assert profile.network_mode == "none"
        assert profile.timeout_seconds == 300
        assert profile.cpu_limit == 2.0
        assert profile.memory_bytes == 3 * 1024 * 1024 * 1024
        assert profile.process_limit == 256
        assert profile.max_output_bytes == 1024 * 1024


def test_discovery_rejects_missing_exact_repository_tag_suffix(tmp_path: Path) -> None:
    plan = build_test_plan()
    engine = _docker(tmp_path)
    missing = sorted(plan.cases, key=lambda case: case.instance_id)[0]
    rows = [
        _row(case.instance_id, f"sha256:{index + 1:064x}")
        for index, case in enumerate(plan.cases)
        if case.instance_id != missing.instance_id
    ]
    rows.extend(
        (
            {
                **_row(missing.instance_id, "sha256:" + "a" * 64),
                "Repository": f"other/sweb.eval.x86_64_{missing.instance_id}",
            },
            {
                **_row(missing.instance_id, "sha256:" + "b" * 64),
                "Tag": "dev",
            },
            {
                **_row(missing.instance_id, "sha256:" + "c" * 64),
                "Repository": f"swebench/sweb.eval.x86_64_{missing.instance_id}-extra",
            },
        )
    )

    with pytest.raises(KernelError) as caught:
        discover_swebench_case_process_profiles(plan, engine, runner=_runner(rows))

    assert caught.value.code == "swebench_public_test_image_missing"


def test_discovery_rejects_duplicate_case_matches(tmp_path: Path) -> None:
    plan = build_test_plan()
    engine = _docker(tmp_path)
    duplicate = plan.cases[0]
    rows = [
        _row(case.instance_id, f"sha256:{index + 1:064x}")
        for index, case in enumerate(plan.cases)
    ]
    rows.append(_row(duplicate.instance_id, "sha256:" + "f" * 64, platform="arm64"))

    with pytest.raises(KernelError) as caught:
        discover_swebench_case_process_profiles(plan, engine, runner=_runner(rows))

    assert caught.value.code == "swebench_public_test_image_ambiguous"


def test_discovery_rejects_noncanonical_local_image_id(tmp_path: Path) -> None:
    plan = build_test_plan()
    engine = _docker(tmp_path)
    rows = [
        _row(case.instance_id, "sha256:short" if index == 0 else f"sha256:{index + 1:064x}")
        for index, case in enumerate(plan.cases)
    ]

    with pytest.raises(KernelError) as caught:
        discover_swebench_case_process_profiles(plan, engine, runner=_runner(rows))

    assert caught.value.code == "swebench_public_test_image_invalid"
