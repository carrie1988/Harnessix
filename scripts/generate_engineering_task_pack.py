"""从受审Benchmark源树确定性生成harnessix-engineering/v2内置Task Pack。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from harnessix.agent.models import Budget
from harnessix.evals.contracts import CodingEvalTask, EvalRepository
from harnessix.evals.task_pack_contracts import (
    CodingEvalReviewFinding,
    CodingEvalTaskPackCase,
    CodingEvalTaskPackLicense,
    CodingEvalTaskPackRepository,
    build_coding_eval_review_oracle,
    build_coding_eval_task_pack,
    build_coding_eval_task_pack_profile,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _PROJECT_ROOT / "benchmarks/taskpacks/harnessix-engineering-v2"
_DEFINITION = _SOURCE_ROOT / "definition.json"
_OUTPUT_ROOT = _PROJECT_ROOT / "src/harnessix/evals/taskpacks/engineering-v2"
_COMMIT_MESSAGE = "Harnessix Eval Task Pack baseline"
_IMAGE_BY_LANGUAGE = {
    "python": "python@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757",
    "javascript": "node@sha256:1b2479dd35a99687d6638f5976fd235e26c5b37e8122f786fcd5fe231d63de5b",
}
_VERSION_BY_LANGUAGE = {
    "python": "3.12.11-alpine3.22",
    "javascript": "22.18.0-alpine3.22",
}
_DEFINITION_KEYS = {
    "pack_id",
    "pack_version",
    "created_at",
    "commit_created_at",
    "repositories",
    "cases",
}
_REPOSITORY_KEYS = {
    "repository_id",
    "language",
    "source_directory",
    "origin",
    "source_kind",
    "spdx_expression",
    "copyright_notice",
    "provenance_uri",
    "license_file",
    "license_sha256",
    "reviewed_at",
}
_CASE_KEYS = {
    "case_id",
    "task_kind",
    "repository_id",
    "profile_id",
    "program",
    "arguments",
    "description",
    "instruction",
    "allowed_changed_paths",
}
_REVIEW_FINDING_KEYS = {
    "finding_id",
    "category",
    "severity",
    "path",
    "start_line",
    "end_line",
}


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "-"
        extra = ",".join(sorted(actual - expected)) or "-"
        raise ValueError(f"{label}字段不匹配：missing={missing}; extra={extra}")


def _load_definition() -> dict[str, Any]:
    value = json.loads(_DEFINITION.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Task Pack生成定义必须是JSON对象")
    definition = cast(dict[str, Any], value)
    _require_exact_keys(definition, _DEFINITION_KEYS, "Task Pack生成定义")
    repositories = definition["repositories"]
    cases = definition["cases"]
    if not isinstance(repositories, list) or not isinstance(cases, list):
        raise ValueError("Task Pack仓库与Case必须是JSON数组")
    for index, item in enumerate(repositories):
        if not isinstance(item, dict):
            raise ValueError(f"Task Pack仓库[{index}]必须是JSON对象")
        _require_exact_keys(item, _REPOSITORY_KEYS, f"Task Pack仓库[{index}]")
    for index, item in enumerate(cases):
        if not isinstance(item, dict):
            raise ValueError(f"Task Pack Case[{index}]必须是JSON对象")
        expected = _CASE_KEYS | ({"review_finding"} if "review_finding" in item else set())
        _require_exact_keys(item, expected, f"Task Pack Case[{index}]")
        if "review_finding" in item:
            finding = item["review_finding"]
            if not isinstance(finding, dict):
                raise ValueError(f"Task Pack Case[{index}] Review Finding必须是JSON对象")
            _require_exact_keys(
                finding,
                _REVIEW_FINDING_KEYS,
                f"Task Pack Case[{index}] Review Finding",
            )
    return definition


def _source_files(root: Path) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Benchmark源目录无效：{root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink() or ".git" in relative.parts:
            raise ValueError(f"Benchmark源树包含禁止路径：{relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Benchmark源树包含非普通文件：{relative.as_posix()}")
        path.as_posix().encode("utf-8", errors="strict")
        files.append(path)
    if not files:
        raise ValueError("Benchmark源树不能为空")
    return tuple(files)


def _archive_bytes(root: Path, files: tuple[Path, ...], mtime: int) -> bytes:
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            body = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(body)
            info.mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = mtime
            archive.addfile(info, io.BytesIO(body))
    return target.getvalue()


def _git_environment(timestamp: str) -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Harnessix Eval",
        "GIT_AUTHOR_EMAIL": "eval@harnessix.invalid",
        "GIT_COMMITTER_NAME": "Harnessix Eval",
        "GIT_COMMITTER_EMAIL": "eval@harnessix.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }


def _run_git(
    git: str, root: Path, arguments: tuple[str, ...], environment: dict[str, str]
) -> bytes:
    result = subprocess.run(
        (git, "-c", "core.autocrlf=false", *arguments),
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"固定Git命令失败：{' '.join(arguments)}：{detail}")
    return result.stdout


def _repository_identity(
    root: Path,
    files: tuple[Path, ...],
    timestamp: str,
) -> tuple[str, str, str, int]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("生成Task Pack需要Git")
    environment = _git_environment(timestamp)
    with tempfile.TemporaryDirectory(prefix="harnessix-task-pack-git-") as directory:
        workspace = Path(directory)
        for source in files:
            relative = source.relative_to(root)
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            target.chmod(0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644)
        _run_git(git, workspace, ("init", "--quiet"), environment)
        _run_git(git, workspace, ("symbolic-ref", "HEAD", "refs/heads/main"), environment)
        _run_git(git, workspace, ("add", "--force", "--all"), environment)
        _run_git(
            git,
            workspace,
            (
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                _COMMIT_MESSAGE,
            ),
            environment,
        )
        revision = (
            _run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{commit}"), environment)
            .decode()
            .strip()
        )
        tree_oid = (
            _run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{tree}"), environment)
            .decode()
            .strip()
        )
        inventory = _run_git(
            git, workspace, ("ls-tree", "-r", "-z", "--full-tree", "HEAD"), environment
        )
    return revision, tree_oid, hashlib.sha256(inventory).hexdigest(), inventory.count(b"\0")


def _review_evidence_sha256(root: Path, finding: dict[str, Any]) -> str:
    expected = root / str(finding["path"])
    path = expected.resolve(strict=True)
    if path != expected or root.resolve(strict=True) not in path.parents:
        raise ValueError("Review Finding路径逃逸Benchmark仓库")
    body = path.read_bytes()
    body.decode("utf-8", errors="strict")
    lines = body.splitlines(keepends=True)
    start = int(finding["start_line"])
    end = int(finding["end_line"])
    if start < 1 or end < start or end > len(lines):
        raise ValueError(f"Review Finding行范围无效：{path}:{start}-{end}")
    return hashlib.sha256(b"".join(lines[start - 1 : end])).hexdigest()


def _generate(target_root: Path) -> None:
    definition = _load_definition()
    target_root.mkdir(parents=True, exist_ok=True)
    archives_root = target_root / "archives"
    archives_root.mkdir()
    timestamp = str(definition["commit_created_at"])
    created_at = datetime.fromisoformat(str(definition["created_at"]).replace("Z", "+00:00"))
    archive_mtime = int(created_at.timestamp())

    repositories: list[CodingEvalTaskPackRepository] = []
    repository_sources: dict[str, Path] = {}
    for source in definition["repositories"]:
        repository_id = str(source["repository_id"])
        root = (_SOURCE_ROOT / str(source["source_directory"])).resolve(strict=True)
        if _SOURCE_ROOT.resolve(strict=True) not in root.parents:
            raise ValueError("Benchmark仓库目录逃逸源根")
        files = _source_files(root)
        license_file = str(source["license_file"])
        license_body = (root / license_file).read_bytes()
        license_sha256 = hashlib.sha256(license_body).hexdigest()
        if license_sha256 != source["license_sha256"]:
            raise ValueError(f"{repository_id}许可证摘要漂移")
        archive = _archive_bytes(root, files, archive_mtime)
        archive_file = f"archives/{repository_id}.tar"
        (target_root / archive_file).write_bytes(archive)
        revision, tree_oid, tree_sha256, tracked_files = _repository_identity(
            root, files, timestamp
        )
        repository = EvalRepository(
            name=repository_id,
            origin=str(source["origin"]),
            source_revision=revision,
            baseline_tree_sha256=tree_sha256,
        )
        repositories.append(
            CodingEvalTaskPackRepository(
                repository_id=repository_id,
                language=str(source["language"]),
                repository=repository,
                source_tree_oid=tree_oid,
                archive_file=archive_file,
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                archive_bytes=len(archive),
                tracked_files=tracked_files,
                commit_created_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                license=CodingEvalTaskPackLicense(
                    source_kind=str(source["source_kind"]),
                    spdx_expression=str(source["spdx_expression"]),
                    copyright_notice=str(source["copyright_notice"]),
                    provenance_uri=str(source["provenance_uri"]),
                    license_file=license_file,
                    license_sha256=license_sha256,
                    reviewed_at=datetime.fromisoformat(
                        str(source["reviewed_at"]).replace("Z", "+00:00")
                    ),
                ),
            )
        )
        repository_sources[repository_id] = root

    repository_by_id = {item.repository_id: item for item in repositories}
    profiles = []
    cases = []
    for source in definition["cases"]:
        case_id = str(source["case_id"])
        repository_id = str(source["repository_id"])
        profile_id = str(source["profile_id"])
        repository = repository_by_id[repository_id]
        profiles.append(
            build_coding_eval_task_pack_profile(
                profile_id=profile_id,
                version=_VERSION_BY_LANGUAGE[repository.language],
                language=repository.language,
                description=str(source["description"]),
                image=_IMAGE_BY_LANGUAGE[repository.language],
                program=str(source["program"]),
                arguments=tuple(str(item) for item in source["arguments"]),
                timeout_seconds=60,
                max_output_bytes=64 * 1024,
                cpu_limit=0.5,
                memory_bytes=128 * 1024 * 1024,
                process_limit=32,
            )
        )
        changed_paths = tuple(str(item) for item in source["allowed_changed_paths"])
        check_id = f"{case_id}-check"
        expected = json.dumps(
            {
                "summary": "实现说明",
                "changed_paths": list(changed_paths),
                "tests": [{"profile": profile_id, "passed": True}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        task = CodingEvalTask(
            task_id=f"engineering-{case_id}",
            task_version=1,
            repository=repository.repository,
            prompt=(
                f"{source['instruction']}只允许修改{', '.join(changed_paths)}，"
                f"运行固定{profile_id}检查，"
                "并核对Git状态和差异。最终回答正文必须且只能是JSON对象，不得包含Markdown围栏或其他文字，"
                f"格式为：{expected}。"
            ),
            allowed_changed_paths=changed_paths,
            required_test_profiles=(profile_id,),
            baseline_checks=(check_id,),
            behavior_checks=(check_id,),
            regression_checks=(),
            max_changed_files=len(changed_paths),
            budget=Budget(
                max_steps=16,
                max_tokens=50_000,
                timeout_seconds=900.0,
                max_output_chars=65_536,
                max_tool_calls_per_step=8,
            ),
        )
        oracle = None
        finding_source = source.get("review_finding")
        if finding_source is not None:
            finding = cast(dict[str, Any], finding_source)
            oracle = build_coding_eval_review_oracle(
                oracle_version=1,
                required_findings=(
                    CodingEvalReviewFinding(
                        finding_id=str(finding["finding_id"]),
                        category=str(finding["category"]),
                        severity=str(finding["severity"]),
                        path=str(finding["path"]),
                        start_line=int(finding["start_line"]),
                        end_line=int(finding["end_line"]),
                        evidence_sha256=_review_evidence_sha256(
                            repository_sources[repository_id], finding
                        ),
                    ),
                ),
            )
        cases.append(
            CodingEvalTaskPackCase(
                case_id=case_id,
                task_kind=str(source["task_kind"]),
                repository_id=repository_id,
                profile_id=profile_id,
                task=task,
                review_oracle=oracle,
            )
        )

    pack = build_coding_eval_task_pack(
        pack_id=str(definition["pack_id"]),
        pack_version=int(definition["pack_version"]),
        repositories=tuple(sorted(repositories, key=lambda item: item.repository_id)),
        profiles=tuple(sorted(profiles, key=lambda item: item.profile_id)),
        cases=tuple(sorted(cases, key=lambda item: item.case_id)),
        created_at=created_at,
    )
    (target_root / "manifest.json").write_text(
        pack.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def _regular_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _check() -> int:
    with tempfile.TemporaryDirectory(prefix="harnessix-task-pack-check-") as directory:
        candidate = Path(directory) / "engineering-v2"
        _generate(candidate)
        expected = _regular_files(candidate)
    actual = _regular_files(_OUTPUT_ROOT)
    if actual == expected:
        return 0
    names = sorted(set(actual) | set(expected))
    for name in names:
        if actual.get(name) != expected.get(name):
            print(f"Task Pack生成制品漂移：{name}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查生成制品是否与源定义一致")
    arguments = parser.parse_args()
    if arguments.check:
        return _check()
    temporary = _OUTPUT_ROOT.with_name(f".{_OUTPUT_ROOT.name}.{os.getpid()}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    try:
        _generate(temporary)
        shutil.rmtree(_OUTPUT_ROOT, ignore_errors=True)
        os.replace(temporary, _OUTPUT_ROOT)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
