from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.public_test_profile import (
    _DJANGO_BOOTSTRAP_ARGUMENT,
    _LEGACY_DJANGO_TEST_ARGUMENTS,
    _LEGACY_PYTEST_ARGUMENTS,
    _PYTEST_BOOTSTRAP_ARGUMENT,
    PUBLIC_TEST_PROFILE_ID,
    SweBenchCaseProcessProfile,
    build_swebench_case_process_profile,
    discover_swebench_case_process_profiles,
)
from harnessix.product_config.action_contracts import build_product_process_profile
from tests.evals.swebench.test_predictions import build_test_plan


def _docker(tmp_path: Path) -> Path:
    engine = tmp_path / "docker"
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o700)
    return engine


def _reseal_profile(binding: dict) -> None:
    fields = binding["profile"].copy()
    fields.pop("spec_version")
    fields.pop("profile_sha256")
    fields["arguments"] = tuple(fields["arguments"])
    fields["secret_refs"] = tuple(fields["secret_refs"])
    binding["profile"] = build_product_process_profile(**fields).model_dump(mode="json")


def _repository(instance_id: str, *, platform: str = "x86_64") -> str:
    owner, repository_case = instance_id.split("__", 1)
    return f"swebench/sweb.eval.{platform}.{owner}_1776_{repository_case}"


def _row(instance_id: str, image_id: str, *, platform: str = "x86_64") -> dict[str, str]:
    return {
        "Repository": _repository(instance_id, platform=platform),
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
            "PYTHONPATH=/workspace",
            "PYTHONDONTWRITEBYTECODE=1",
            "/opt/miniconda3/envs/testbed/bin/python",
            "-B",
            "-c",
            _PYTEST_BOOTSTRAP_ARGUMENT,
            "/workspace",
            "/testbed",
            "-q",
            "-p",
            "no:cacheprovider",
            "--disable-warnings",
            "--tb=short",
            "--maxfail=1",
            "--",
        )
        assert profile.selector_policy == "bounded_test_selector"
        assert profile.network_mode == "none"
        assert profile.timeout_seconds == 300
        assert profile.cpu_limit == 2.0
        assert profile.memory_bytes == 3 * 1024 * 1024 * 1024
        assert profile.process_limit == 256
        assert profile.max_output_bytes == 1024 * 1024


def test_pytest_bootstrap_uses_workspace_source_and_image_generated_fallback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    image_workspace = tmp_path / "image"
    (workspace / "src" / "example").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (image_workspace / "src" / "example").mkdir(parents=True)
    (workspace / "src" / "example" / "__init__.py").write_text(
        "from .generated import VALUE\n",
        encoding="utf-8",
    )
    (image_workspace / "src" / "example" / "__init__.py").write_text("", encoding="utf-8")
    (image_workspace / "src" / "example" / "generated.py").write_text(
        "VALUE = 'from-image-build'\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_example.py").write_text(
        "from pathlib import Path\n"
        "from example import VALUE\n\n"
        "def test_workspace_with_generated_fallback():\n"
        "    assert VALUE == 'from-image-build'\n"
        "    assert Path.cwd().resolve() == Path(__file__).parent.parent.resolve()\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-c",
            _PYTEST_BOOTSTRAP_ARGUMENT,
            str(workspace),
            str(image_workspace),
            "-q",
            "-p",
            "no:cacheprovider",
            "--",
            "tests/test_example.py",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_pytest_bootstrap_preserves_explicit_node_and_relative_repository_fixtures(tmp_path):
    workspace, image_workspace = tmp_path / "workspace", tmp_path / "image"
    (workspace / "tests").mkdir(parents=True)
    image_workspace.mkdir()
    (workspace / "requirements-dev.txt").write_text("public fixture", encoding="utf-8")
    target = workspace / "tests" / "test_feature.py"
    target.write_text(
        "from pathlib import Path\n"
        "def test_selected():\n"
        "    assert Path('requirements-dev.txt').read_text() == 'public fixture'\n"
        "def test_unselected():\n"
        "    raise AssertionError('selector node was lost')\n",
        encoding="utf-8",
    )
    before = {str(path): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-c",
            _PYTEST_BOOTSTRAP_ARGUMENT,
            str(workspace),
            str(image_workspace),
            "-q",
            "-p",
            "no:cacheprovider",
            "--",
            "tests/test_feature.py::test_selected",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    assert before == {
        str(path): path.read_bytes() for path in workspace.rglob("*") if path.is_file()
    }


def test_pytest_bootstrap_replaces_missing_selector_with_changed_file_tests(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    image_workspace = tmp_path / "image"
    (workspace / "src" / "example").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (image_workspace / "src" / "example").mkdir(parents=True)
    (workspace / "src" / "example" / "__init__.py").write_text("", encoding="utf-8")
    feature = workspace / "src" / "example" / "feature.py"
    feature.write_text("VALUE = 'before'\n", encoding="utf-8")
    (workspace / "tests" / "test_feature.py").write_text(
        "from example.feature import VALUE\n\ndef test_feature():\n    assert VALUE == 'after'\n",
        encoding="utf-8",
    )
    (image_workspace / "src" / "example" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(("/usr/bin/git", "init", "-q", str(workspace)), check=True)
    subprocess.run(("/usr/bin/git", "-C", str(workspace), "add", "."), check=True)
    subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Harnessix Tests",
            "-c",
            "user.email=tests@harnessix.invalid",
            "commit",
            "-qm",
            "baseline",
        ),
        check=True,
    )
    feature.write_text("VALUE = 'after'\n", encoding="utf-8")

    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-c",
            _PYTEST_BOOTSTRAP_ARGUMENT,
            str(workspace),
            str(image_workspace),
            "-q",
            "-p",
            "no:cacheprovider",
            "--",
            "tests/test_missing.py::test_guessed_node",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout


def test_django_profile_uses_repository_native_runner_and_workspace_imports(
    tmp_path: Path,
) -> None:
    engine = _docker(tmp_path)

    profile = build_swebench_case_process_profile(
        "django__django-11734",
        repo="django/django",
        container_engine=engine,
        image_id="sha256:" + "a" * 64,
    ).profile

    assert profile.arguments[-5:] == (
        "/opt/miniconda3/envs/testbed/bin/python",
        "-B",
        "-c",
        _DJANGO_BOOTSTRAP_ARGUMENT,
        "/workspace",
    )
    assert "PYTHONPATH=/workspace" in profile.arguments
    assert profile.version == "swebench-public-tests-v7"


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("tests/feature", "feature"),
        ("tests/feature/tests.py", "feature.tests"),
        ("tests/feature/tests.py::Tests::test_case", "feature.tests.Tests.test_case"),
        ("feature.tests.Tests.test_case", "feature.tests.Tests.test_case"),
    ],
)
def test_django_bootstrap_converts_generic_selectors_and_preserves_runner_exit(
    tmp_path: Path, selector: str, expected: str
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "runtests.py").write_text(
        "import json,sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
        "assert sys.path[0] == __file__.rsplit('/', 1)[0]\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (sys.executable, "-B", "-c", _DJANGO_BOOTSTRAP_ARGUMENT, str(tmp_path), selector),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 7, completed.stderr
    assert json.loads(completed.stdout) == ["--parallel=1", expected]


@pytest.mark.parametrize("selector", ["../feature", "--parallel=2", "tests/", "feature::"])
def test_django_bootstrap_rejects_invalid_label_before_native_runner(
    tmp_path: Path, selector: str
) -> None:
    completed = subprocess.run(
        (sys.executable, "-B", "-c", _DJANGO_BOOTSTRAP_ARGUMENT, str(tmp_path), selector),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "not a native test label" in completed.stderr
    assert "FileNotFoundError" not in completed.stderr


@pytest.mark.parametrize("repo", ["django/django", "example/example"])
@pytest.mark.parametrize("version", ["swebench-public-tests-v5", "swebench-public-tests-v6"])
def test_frozen_profile_is_read_without_silent_upgrade(tmp_path: Path, repo: str, version) -> None:
    binding = build_swebench_case_process_profile(
        "example__example-1",
        repo=repo,
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
    ).model_dump(mode="json")
    binding["profile"]["version"] = version
    if repo == "django/django" and version == "swebench-public-tests-v5":
        binding["profile"]["arguments"] = list(_LEGACY_DJANGO_TEST_ARGUMENTS)
    elif repo != "django/django":
        binding["profile"]["arguments"] = list(_LEGACY_PYTEST_ARGUMENTS)
    _reseal_profile(binding)
    frozen_bytes = json.dumps(binding)

    restored = SweBenchCaseProcessProfile.model_validate_json(frozen_bytes)

    assert restored.model_dump(mode="json") == binding
    assert restored.profile.version == version


@pytest.mark.parametrize(
    "version,legacy_runner",
    [
        ("swebench-public-tests-v5", False),
        ("swebench-public-tests-v6", True),
        ("swebench-public-tests-v7", True),
        ("swebench-public-tests-v8", False),
    ],
)
def test_django_profile_rejects_version_and_fixed_runner_argument_drift(
    tmp_path: Path, version: str, legacy_runner: bool
) -> None:
    binding = build_swebench_case_process_profile(
        "example__example-1",
        repo="django/django",
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
    ).model_dump(mode="json")
    binding["profile"]["version"] = version
    if legacy_runner:
        binding["profile"]["arguments"] = list(_LEGACY_DJANGO_TEST_ARGUMENTS)
    _reseal_profile(binding)

    with pytest.raises(ValueError, match="固定执行边界"):
        SweBenchCaseProcessProfile.model_validate_json(json.dumps(binding))


@pytest.mark.parametrize("version", ["swebench-public-tests-v5", "swebench-public-tests-v6"])
def test_legacy_pytest_profile_does_not_silently_take_new_cwd_and_node_semantics(tmp_path, version):
    binding = build_swebench_case_process_profile(
        "example__example-1",
        repo="example/example",
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
    ).model_dump(mode="json")
    binding["profile"]["version"] = version
    _reseal_profile(binding)
    with pytest.raises(ValueError, match="固定执行边界"):
        SweBenchCaseProcessProfile.model_validate_json(json.dumps(binding))


def test_sympy_profile_uses_repository_native_runner_and_workspace_imports(
    tmp_path: Path,
) -> None:
    engine = _docker(tmp_path)

    profile = build_swebench_case_process_profile(
        "sympy__sympy-20801",
        repo="sympy/sympy",
        container_engine=engine,
        image_id="sha256:" + "b" * 64,
    ).profile

    assert profile.arguments[-3:] == (
        "/opt/miniconda3/envs/testbed/bin/python",
        "-B",
        "bin/test",
    )
    assert "PYTHONPATH=/workspace" in profile.arguments


def test_discovery_rejects_missing_exact_repository_and_tag(tmp_path: Path) -> None:
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
                "Repository": _repository(missing.instance_id).replace("swebench/", "other/"),
            },
            {
                **_row(missing.instance_id, "sha256:" + "b" * 64),
                "Tag": "dev",
            },
            {
                **_row(missing.instance_id, "sha256:" + "c" * 64),
                "Repository": _repository(missing.instance_id) + "-extra",
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
        _row(case.instance_id, f"sha256:{index + 1:064x}") for index, case in enumerate(plan.cases)
    ]
    rows.append(_row(duplicate.instance_id, "sha256:" + "f" * 64))

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


def test_snapshot_bootstrap_runs_on_a_writable_copy_and_leaves_workspace_untouched(
    tmp_path, monkeypatch
):
    from harnessix.evals.swebench.public_test_profile import _SNAPSHOT_PYTEST_ARGUMENTS

    workspace = tmp_path / "workspace"
    image_workspace = tmp_path / "image"
    (workspace / "src" / "example").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (image_workspace / "src" / "example").mkdir(parents=True)
    (workspace / "src" / "example" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "tests" / "test_example.py").write_text(
        "from pathlib import Path\n"
        "import example\n\n"
        "def test_writes_next_to_itself_and_imports_the_copy():\n"
        "    (Path(__file__).parent / 'result_images').mkdir()\n"
        "    (Path.cwd() / 'written.txt').write_text('x')\n"
        "    assert Path(example.__file__).resolve().is_relative_to(Path.cwd().resolve())\n"
        "    assert Path.cwd().resolve() == Path(__file__).parent.parent.resolve()\n",
        encoding="utf-8",
    )
    for path in sorted(workspace.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    workspace.chmod(0o555)
    bootstrap = next(a for a in _SNAPSHOT_PYTEST_ARGUMENTS if a.startswith("exec("))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    try:
        completed = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                bootstrap,
                str(workspace),
                str(image_workspace),
                "-q",
                "-p",
                "no:cacheprovider",
                "--",
                "tests/test_example.py",
            ),
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(workspace)},
        )
    finally:
        for path in [workspace, *workspace.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (workspace / "tests" / "result_images").exists()
    assert not (workspace / "written.txt").exists()


def test_snapshot_profile_is_a_distinct_version_with_a_larger_tmpfs(tmp_path):
    default = build_swebench_case_process_profile(
        "example__example-1",
        repo="example/example",
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
    )
    snapshot = build_swebench_case_process_profile(
        "example__example-1",
        repo="example/example",
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
        writable_snapshot=True,
    )
    assert default.profile.version == "swebench-public-tests-v7"
    assert default.profile.tmpfs_bytes is None
    assert "tmpfs_bytes" not in default.profile.model_dump(mode="json")
    assert snapshot.profile.version == "swebench-public-tests-v8"
    assert snapshot.profile.tmpfs_bytes == 1024**3
    assert SweBenchCaseProcessProfile.model_validate_json(snapshot.model_dump_json()) == snapshot
    binding = snapshot.model_dump(mode="json")
    binding["profile"].pop("tmpfs_bytes")
    _reseal_profile(binding)
    with pytest.raises(ValueError, match="固定执行边界"):
        SweBenchCaseProcessProfile.model_validate_json(json.dumps(binding))


def _native_selector_argument(repo: str) -> str:
    from harnessix.evals.swebench.public_test_profile import _profile_arguments

    return _profile_arguments(repo, version="swebench-public-tests-v9")[
        -2 if repo == "django/django" else -1
    ]


def test_v9_django_bootstrap_rejects_empty_selectors_with_an_explicit_reason(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "runtests.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    argument = _native_selector_argument("django/django")
    empty = subprocess.run(
        (sys.executable, "-B", "-c", argument, str(tmp_path)),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert empty.returncode != 0 and "needs explicit selectors" in empty.stderr
    labelled = subprocess.run(
        (sys.executable, "-B", "-c", argument, str(tmp_path), "tests/feature/tests.py"),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert labelled.returncode == 0, labelled.stderr


def test_v9_sympy_bootstrap_rejects_empty_selectors_and_runs_bin_test(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    # 真实bin/test导入同目录的get_sympy（v61中缺少脚本目录导致所有SymPy测试启动失败）。
    (tmp_path / "bin" / "get_sympy.py").write_text("path_hack = None\n", encoding="utf-8")
    (tmp_path / "bin" / "test").write_text(
        "import json,sys\nfrom get_sympy import path_hack\n"
        "print(json.dumps(sys.argv))\nraise SystemExit(5)\n",
        encoding="utf-8",
    )
    argument = _native_selector_argument("sympy/sympy")
    empty = subprocess.run(
        (sys.executable, "-B", "-c", argument),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert empty.returncode != 0 and "needs explicit selectors" in empty.stderr
    selected = subprocess.run(
        (sys.executable, "-B", "-c", argument, "sympy/core/tests/test_basic.py"),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert selected.returncode == 5
    assert json.loads(selected.stdout) == ["bin/test", "sympy/core/tests/test_basic.py"]


def test_v9_profile_is_frozen_only_over_the_writable_snapshot(tmp_path: Path) -> None:
    v9 = build_swebench_case_process_profile(
        "django__django-1",
        repo="django/django",
        container_engine=_docker(tmp_path),
        image_id="sha256:" + "a" * 64,
        writable_snapshot=True,
        require_native_selectors=True,
    )
    assert v9.profile.version == "swebench-public-tests-v9"
    assert v9.profile.tmpfs_bytes == 1024**3
    assert SweBenchCaseProcessProfile.model_validate_json(v9.model_dump_json()) == v9
    with pytest.raises(ValueError):
        build_swebench_case_process_profile(
            "django__django-1",
            repo="django/django",
            container_engine=_docker(tmp_path),
            image_id="sha256:" + "a" * 64,
            require_native_selectors=True,
        )
