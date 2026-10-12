from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import harnessix.evals.swebench.workspace as workspace_module
from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.contracts import SweBenchPublicCase
from harnessix.evals.swebench.workspace import materialize_swebench_workspace

GIT = Path(shutil.which("git") or "/usr/bin/git").resolve()
PUBLIC_REPOSITORY_URL = "https://github.com/owner/repository.git"


def _run_git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        (str(GIT), *arguments),
        cwd=repository,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def public_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "public-repository"
    repository.mkdir(mode=0o700)
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "config", "user.name", "Harnessix Tests")
    _run_git(repository, "config", "user.email", "tests@harnessix.invalid")
    (repository / "README.md").write_text("public baseline\n", encoding="utf-8")
    (repository / ".gitmodules").write_text(
        '[submodule "dependency"]\n'
        "\tpath = vendor/dependency\n"
        "\turl = https://github.com/public/dependency.git\n",
        encoding="utf-8",
    )
    _run_git(repository, "add", "README.md", ".gitmodules")
    _run_git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'1' * 40},vendor/dependency",
    )
    _run_git(repository, "commit", "--quiet", "-m", "public baseline")
    base_commit = _run_git(repository, "rev-parse", "HEAD")
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    _run_git(repository, "add", "later.txt")
    _run_git(repository, "commit", "--quiet", "-m", "later")
    return repository, base_commit, _run_git(repository, "rev-parse", "HEAD")


@pytest.fixture
def local_public_transport(
    monkeypatch: pytest.MonkeyPatch,
    public_repository: tuple[Path, str, str],
) -> None:
    repository, _, _ = public_repository
    monkeypatch.setattr(
        workspace_module,
        "_GIT_GLOBAL_ARGUMENTS",
        (
            *workspace_module._GIT_GLOBAL_ARGUMENTS,
            "-c",
            "protocol.file.allow=always",
            "-c",
            f"url.{repository.as_uri()}.insteadOf={PUBLIC_REPOSITORY_URL}",
        ),
    )


def _case(base_commit: str, *, instance_id: str = "owner__repository-1") -> SweBenchPublicCase:
    return SweBenchPublicCase(
        instance_id=instance_id,
        repo="owner/repository",
        base_commit=base_commit,
        problem_statement="Fix a public issue",
        version="1.0",
    )


def _materialize(
    tmp_path: Path,
    case: SweBenchPublicCase,
    *,
    allow_network: bool,
    require_clean: bool = True,
) -> Path:
    return materialize_swebench_workspace(
        case,
        workspaces_root=tmp_path / "workspaces",
        cache_root=tmp_path / "cache",
        git_executable=GIT,
        allow_network=allow_network,
        require_clean=require_clean,
    )


def _network_commands(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [arguments for arguments in calls if "clone" in arguments or "fetch" in arguments]


def test_first_materialization_uses_fixed_git_environment_and_detached_checkout(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    local_public_transport: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base_commit, _ = public_repository
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def observed_run(
        arguments: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        assert kwargs["shell"] is False
        assert kwargs["env"] == workspace_module._GIT_ENVIRONMENT
        return real_run(arguments, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(workspace_module.subprocess, "run", observed_run)

    materialized = _materialize(tmp_path, _case(base_commit), allow_network=True)
    monkeypatch.setattr(workspace_module.subprocess, "run", real_run)
    caches = list((tmp_path / "cache").glob("*.git"))

    assert materialized == (tmp_path / "workspaces" / "owner__repository-1").resolve()
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "workspaces").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700
    assert len(caches) == 1 and stat.S_IMODE(caches[0].stat().st_mode) == 0o700
    assert _run_git(materialized, "rev-parse", "HEAD") == base_commit
    assert _run_git(materialized, "branch", "--show-current") == ""
    assert _run_git(materialized, "remote", "get-url", "origin") == PUBLIC_REPOSITORY_URL
    assert _run_git(materialized, "status", "--porcelain") == ""
    assert not (materialized / ".git" / "modules").exists()
    assert not (materialized / "vendor" / "dependency" / ".git").exists()
    assert (materialized / ".git" / "objects" / "info" / "alternates").read_text() == (
        f"{caches[0].resolve() / 'objects'}\n"
    )
    assert sum("clone" in arguments for arguments in calls) == 1
    assert all(isinstance(arguments, tuple) for arguments in calls)


def test_recovery_is_idempotent_and_existing_cache_materializes_offline_case(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    local_public_transport: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base_commit, _ = public_repository
    first = _materialize(tmp_path, _case(base_commit), allow_network=True)
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def observed_run(
        arguments: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        return real_run(arguments, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(workspace_module.subprocess, "run", observed_run)

    reopened = _materialize(tmp_path, _case(base_commit), allow_network=False)
    second = _materialize(
        tmp_path,
        _case(base_commit, instance_id="owner__repository-2"),
        allow_network=False,
    )

    assert reopened == first
    assert second.name == "owner__repository-2"
    assert _run_git(second, "rev-parse", "HEAD") == base_commit
    assert _network_commands(calls) == []
    assert len(list((tmp_path / "cache").glob("*.git"))) == 1


@pytest.mark.parametrize("drift", ["head", "dirty", "origin"])
def test_recovery_fails_closed_on_repository_drift(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    local_public_transport: None,
    drift: str,
) -> None:
    _, base_commit, later_commit = public_repository
    case = _case(base_commit)
    materialized = _materialize(tmp_path, case, allow_network=True)
    if drift == "head":
        _run_git(materialized, "checkout", "--quiet", "--detach", later_commit)
    elif drift == "dirty":
        (materialized / "README.md").write_text("changed\n", encoding="utf-8")
    else:
        _run_git(materialized, "remote", "set-url", "origin", "https://example.invalid/repo.git")

    with pytest.raises(KernelError) as caught:
        _materialize(tmp_path, case, allow_network=True)

    assert caught.value.code == "swebench_workspace_changed"


def test_authenticated_run_can_reopen_dirty_workspace_without_relaxing_identity(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    local_public_transport: None,
) -> None:
    _, base_commit, _ = public_repository
    case = _case(base_commit)
    materialized = _materialize(tmp_path, case, allow_network=True)
    (materialized / "README.md").write_text("agent patch\n", encoding="utf-8")

    reopened = _materialize(
        tmp_path,
        case,
        allow_network=False,
        require_clean=False,
    )

    assert reopened == materialized
    assert _run_git(reopened, "rev-parse", "HEAD") == base_commit
    assert _run_git(reopened, "status", "--porcelain") == "M README.md"


def test_recovery_rejects_workspace_directory_type_drift(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    local_public_transport: None,
) -> None:
    _, base_commit, _ = public_repository
    case = _case(base_commit)
    materialized = _materialize(tmp_path, case, allow_network=True)
    moved = materialized.with_name("moved-workspace")
    materialized.rename(moved)
    materialized.symlink_to(moved, target_is_directory=True)

    with pytest.raises(KernelError) as caught:
        _materialize(tmp_path, case, allow_network=False)

    assert caught.value.code == "swebench_workspace_changed"


def test_network_disabled_without_cache_never_clones_or_fetches(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base_commit, _ = public_repository
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def observed_run(
        arguments: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        return real_run(arguments, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(workspace_module.subprocess, "run", observed_run)

    with pytest.raises(KernelError) as caught:
        _materialize(tmp_path, _case(base_commit), allow_network=False)

    assert caught.value.code == "swebench_workspace_network_disabled"
    assert _network_commands(calls) == []


@pytest.mark.parametrize("kind", ["missing", "directory", "not-executable"])
def test_invalid_git_executable_is_rejected_before_materialization(
    tmp_path: Path,
    public_repository: tuple[Path, str, str],
    kind: str,
) -> None:
    _, base_commit, _ = public_repository
    executable = tmp_path / "invalid-git"
    if kind == "directory":
        executable.mkdir()
    elif kind == "not-executable":
        executable.write_text("not git\n", encoding="utf-8")
        executable.chmod(0o600)

    with pytest.raises(KernelError) as caught:
        materialize_swebench_workspace(
            _case(base_commit),
            workspaces_root=tmp_path / "workspaces",
            cache_root=tmp_path / "cache",
            git_executable=executable,
            allow_network=True,
        )

    assert caught.value.code == "swebench_git_invalid"
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "cache").exists()
