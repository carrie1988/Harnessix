from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.suite_contracts import EVAL_TASK_KINDS
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_materializer import (
    _verify_review_oracle,
    materialize_task_pack_case,
)

_PROJECT_ROOT = Path(__file__).parents[2]
_SOLUTIONS = _PROJECT_ROOT / "benchmarks/taskpacks/harnessix-engineering-v1/solutions"


def _git() -> Path:
    executable = shutil.which("git")
    assert executable is not None
    return Path(executable)


def _materialize(tmp_path: Path, case_id: str):
    root = tmp_path / "runs"
    root.mkdir(mode=0o700, exist_ok=True)
    return materialize_task_pack_case(
        builtin_coding_eval_task_pack("harnessix-engineering", 1),
        root,
        _git(),
        case_id,
        uuid4(),
    )


def _host_check(workspace: Path, profile_id: str) -> subprocess.CompletedProcess[bytes]:
    pack = builtin_coding_eval_task_pack("harnessix-engineering", 1).manifest
    profile = pack.profile(profile_id)
    if profile.language == "python":
        command = (sys.executable, *profile.arguments)
    else:
        node = shutil.which("node")
        if node is None:
            pytest.skip("宿主未安装Node，JavaScript真实检查由固定Container CI覆盖")
        command = (node, *profile.arguments)
    return subprocess.run(
        command,
        cwd=workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )


def test_engineering_pack_has_balanced_production_scope() -> None:
    pack = builtin_coding_eval_task_pack("harnessix-engineering", 1).manifest

    assert pack.pack_id == "harnessix-engineering" and pack.pack_version == 1
    assert len(pack.repositories) == 3
    assert len(pack.cases) == 10
    assert Counter(case.task_kind for case in pack.cases) == Counter(
        {kind: 2 for kind in EVAL_TASK_KINDS}
    )
    assert {item.language for item in pack.repositories} == {"python", "javascript"}
    assert all(item.license.source_kind == "third_party" for item in pack.repositories)
    assert all(item.license.spdx_expression == "MIT" for item in pack.repositories)
    assert all("/tree/" in item.license.provenance_uri for item in pack.repositories)
    assert len(pack.profiles) == len(pack.cases)
    assert sum(case.review_oracle is not None for case in pack.cases) == 2


def test_engineering_pack_generation_is_reproducible() -> None:
    result = subprocess.run(
        (sys.executable, "scripts/generate_engineering_task_pack.py", "--check"),
        cwd=_PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")


def test_golden_solutions_are_excluded_from_installable_artifacts() -> None:
    configuration = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hatch = configuration["tool"]["hatch"]["build"]["targets"]

    assert hatch["wheel"]["packages"] == ["src/harnessix"]
    assert "benchmarks/taskpacks/*/solutions" in hatch["sdist"]["exclude"]


def test_engineering_pack_materializes_every_case_and_binds_review_oracles(
    tmp_path: Path,
) -> None:
    pack = builtin_coding_eval_task_pack("harnessix-engineering", 1).manifest
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)

    for case in pack.cases:
        item = materialize_task_pack_case(
            builtin_coding_eval_task_pack("harnessix-engineering", 1),
            root,
            _git(),
            case.case_id,
            uuid4(),
        )
        assert item.manifest.pack_sha256 == pack.pack_sha256
        assert item.manifest.case_id == case.case_id
        assert item.manifest.repository_id == case.repository_id


@pytest.mark.parametrize(
    "case_id",
    [
        "agents-dump-compatible-refactor",
        "agents-normalize-tool-name",
        "agents-payload-bytes-test",
        "agents-secret-redaction-review",
        "langchain-batch-none-test",
        "langchain-storage-replacement",
        "langchain-stringify-dict-keys",
        "opencode-path-normalization",
        "opencode-retry-delay-refactor",
        "opencode-terminal-url-review",
    ],
)
def test_each_engineering_case_fails_then_golden_patch_passes(
    tmp_path: Path,
    case_id: str,
) -> None:
    item = _materialize(tmp_path, case_id)
    baseline = _host_check(item.workspace, item.case.profile_id)
    assert baseline.returncode != 0

    patch = _SOLUTIONS / f"{case_id}.patch"
    applied = subprocess.run(
        (_git(), "apply", "--unidiff-zero", "--whitespace=nowarn", str(patch)),
        cwd=item.workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    assert applied.returncode == 0, applied.stdout.decode("utf-8", errors="replace")
    final = _host_check(item.workspace, item.case.profile_id)
    assert final.returncode == 0, final.stdout.decode("utf-8", errors="replace")
    status = subprocess.check_output(
        (_git(), "status", "--short", "--untracked-files=all"),
        cwd=item.workspace,
        text=True,
    )
    changed_paths = tuple(line[3:] for line in status.splitlines() if line)
    assert item.case.task.allowed_changed_paths == changed_paths


def test_review_oracle_rejects_changed_source_evidence(tmp_path: Path) -> None:
    item = _materialize(tmp_path, "agents-secret-redaction-review")
    target = item.workspace / "src/agent_utils.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "    return value\n", "    return value.strip()\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(KernelError) as mismatch:
        _verify_review_oracle(item.workspace, item.case)
    assert mismatch.value.code == "eval_task_pack_review_oracle_invalid"
