from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from importlib import resources
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals import task_pack as task_pack_module
from harnessix.evals.task_pack import (
    LoadedCodingEvalTaskPack,
    build_task_pack_product_profile,
    builtin_coding_eval_task_pack,
    builtin_coding_eval_task_pack_ids,
    builtin_coding_eval_task_pack_versions,
)
from harnessix.evals.task_pack_contracts import (
    CodingEvalReviewFinding,
    CodingEvalReviewOracle,
    CodingEvalTaskPack,
    CodingEvalTaskPackMaterialization,
    build_coding_eval_review_oracle,
)
from harnessix.evals.task_pack_materializer import (
    _safe_archive_members,
    load_materialized_task_pack_case,
    materialize_task_pack_case,
)


def _git() -> Path:
    executable = shutil.which("git")
    assert executable is not None
    return Path(executable)


def _runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    return root


def _materialize(tmp_path: Path, case_id: str, run_id: UUID | None = None):
    loaded = builtin_coding_eval_task_pack()
    return materialize_task_pack_case(
        loaded,
        _runs_root(tmp_path),
        _git(),
        case_id,
        run_id or uuid4(),
    )


def _git_text(workspace: Path, *arguments: str) -> str:
    return subprocess.check_output((_git(), *arguments), cwd=workspace, text=True).strip()


def _manifest_data() -> dict[str, object]:
    return json.loads(builtin_coding_eval_task_pack().manifest.model_dump_json())


def test_builtin_task_pack_is_versioned_canonical_and_multi_language() -> None:
    loaded = builtin_coding_eval_task_pack()
    pack = loaded.manifest

    assert builtin_coding_eval_task_pack_ids() == (
        "harnessix-engineering",
        "harnessix-seed",
    )
    assert builtin_coding_eval_task_pack_versions("harnessix-engineering") == (1, 2)
    assert builtin_coding_eval_task_pack_versions("harnessix-seed") == (1,)
    assert builtin_coding_eval_task_pack("harnessix-engineering").manifest.pack_version == 2
    assert builtin_coding_eval_task_pack("harnessix-engineering", 1).manifest.pack_version == 1
    assert pack.pack_id == "harnessix-seed" and pack.pack_version == 1
    assert [item.repository_id for item in pack.repositories] == [
        "javascript-slug",
        "python-mathbox",
    ]
    assert {item.language for item in pack.repositories} == {"javascript", "python"}
    assert [item.case_id for item in pack.cases] == [
        "javascript-slug-lowercase",
        "python-mathbox-addition",
    ]
    assert all(item.network_mode == "none" and "@sha256:" in item.image for item in pack.profiles)
    assert all(case.task.required_test_profiles == (case.profile_id,) for case in pack.cases)
    assert all(case.task.baseline_checks == case.task.behavior_checks for case in pack.cases)

    with pytest.raises(KernelError) as missing:
        builtin_coding_eval_task_pack("missing")
    assert missing.value.code == "eval_task_pack_not_found"
    with pytest.raises(KernelError) as version:
        builtin_coding_eval_task_pack("harnessix-seed", 2)
    assert version.value.code == "eval_task_pack_not_found"
    with pytest.raises(KernelError) as versions:
        builtin_coding_eval_task_pack_versions("missing")
    assert versions.value.code == "eval_task_pack_not_found"


def test_task_pack_contract_rejects_dynamic_or_inconsistent_execution() -> None:
    data = _manifest_data()
    profiles = data["profiles"]
    assert isinstance(profiles, list)
    profiles[0]["image"] = "node:latest"
    with pytest.raises(ValidationError):
        CodingEvalTaskPack.model_validate_json(json.dumps(data), strict=True)

    data = _manifest_data()
    profiles = data["profiles"]
    assert isinstance(profiles, list)
    profiles[0]["network_mode"] = "host"
    with pytest.raises(ValidationError):
        CodingEvalTaskPack.model_validate_json(json.dumps(data), strict=True)

    data = _manifest_data()
    cases = data["cases"]
    assert isinstance(cases, list)
    cases[0]["profile_id"] = "python-unittest"
    data["pack_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="语言与仓库不一致"):
        CodingEvalTaskPack.model_validate_json(json.dumps(data), strict=True)

    data = _manifest_data()
    repositories = data["repositories"]
    assert isinstance(repositories, list)
    repositories[1] = repositories[0]
    data["pack_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="唯一排序"):
        CodingEvalTaskPack.model_validate_json(json.dumps(data), strict=True)

    data = _manifest_data()
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        CodingEvalTaskPack.model_validate_json(json.dumps(data), strict=True)


def test_review_oracle_is_deterministic_and_required_only_for_review_cases() -> None:
    finding = CodingEvalReviewFinding(
        finding_id="missing-validation",
        category="correctness",
        severity="high",
        path="src/slug.mjs",
        start_line=1,
        end_line=2,
        evidence_sha256="1" * 64,
    )
    oracle = build_coding_eval_review_oracle(
        oracle_version=1,
        required_findings=(finding,),
    )
    assert (
        CodingEvalReviewOracle.model_validate_json(oracle.model_dump_json(), strict=True) == oracle
    )

    body = json.loads(oracle.model_dump_json())
    body["oracle_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="摘要不一致"):
        CodingEvalReviewOracle.model_validate_json(json.dumps(body), strict=True)

    case = builtin_coding_eval_task_pack().manifest.cases[0]
    with pytest.raises(ValidationError, match="Review任务"):
        case.model_validate({**case.model_dump(), "task_kind": "review"}, strict=True)
    with pytest.raises(ValidationError, match="Review任务"):
        case.model_validate({**case.model_dump(), "review_oracle": oracle}, strict=True)


def test_loader_rejects_root_symlink_and_tampered_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_module = tmp_path / "task_pack.py"
    fake_module.write_text("", encoding="utf-8")
    catalog = tmp_path / "taskpacks"
    catalog.mkdir()
    real = catalog / "real"
    real.mkdir()
    (catalog / "linked").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(task_pack_module, "__file__", str(fake_module))

    with pytest.raises(KernelError) as symlink:
        task_pack_module._load_builtin_directory("linked")
    assert symlink.value.code == "eval_task_pack_invalid"

    source = Path(__file__).parents[2] / "src/harnessix/evals/taskpacks/v1"
    copied = catalog / "copied"
    shutil.copytree(source, copied)
    archive = copied / "archives/javascript-slug.tar"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(KernelError) as tampered:
        task_pack_module._load_builtin_directory("copied")
    assert tampered.value.code == "eval_task_pack_archive_invalid"


def test_consumers_reject_forged_loaded_pack(tmp_path: Path) -> None:
    real = builtin_coding_eval_task_pack()
    forged_root = tmp_path / "forged"
    forged_root.mkdir()
    forged = LoadedCodingEvalTaskPack(real.manifest, forged_root)

    with pytest.raises(KernelError) as profile:
        build_task_pack_product_profile(forged, "python-unittest", _git())
    assert profile.value.code == "eval_task_pack_invalid"

    with pytest.raises(KernelError) as materialization:
        materialize_task_pack_case(
            forged,
            _runs_root(tmp_path),
            _git(),
            "python-mathbox-addition",
            uuid4(),
        )
    assert materialization.value.code == "eval_task_pack_invalid"


def test_archive_policy_rejects_traversal_links_and_special_members() -> None:
    repository = builtin_coding_eval_task_pack().manifest.repositories[0]
    for member in (
        tarfile.TarInfo("../escape"),
        tarfile.TarInfo("link"),
        tarfile.TarInfo("device"),
        tarfile.TarInfo(".GIT/config"),
        tarfile.TarInfo("path\\with-backslash"),
        tarfile.TarInfo("path//with-empty-part"),
        tarfile.TarInfo("path/with\tcontrol"),
    ):
        member.size = 0
        if member.name == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "LICENSE"
        elif member.name == "device":
            member.type = tarfile.CHRTYPE
        else:
            member.type = tarfile.REGTYPE
            member.mode = 0o644
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as output:
            output.addfile(member, io.BytesIO(b"") if member.isfile() else None)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r:") as archive:
            with pytest.raises(ValueError):
                _safe_archive_members(archive, repository)

    duplicate = io.BytesIO()
    with tarfile.open(fileobj=duplicate, mode="w", format=tarfile.USTAR_FORMAT) as output:
        for _ in range(2):
            member = tarfile.TarInfo("duplicate.txt")
            member.mode = 0o644
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
    duplicate.seek(0)
    with tarfile.open(fileobj=duplicate, mode="r:") as archive:
        with pytest.raises(ValueError):
            _safe_archive_members(archive, repository)


def test_materializes_exact_single_commit_reopens_dirty_workspace(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack()
    runs = _runs_root(tmp_path)
    run_id = uuid4()
    first = materialize_task_pack_case(
        loaded,
        runs,
        _git(),
        "python-mathbox-addition",
        run_id,
    )
    repository = loaded.manifest.repository(first.case.repository_id)

    assert first.run_root.stat().st_mode & 0o777 == 0o700
    assert first.workspace.stat().st_mode & 0o777 == 0o755
    assert (first.run_root / "materialization.json").stat().st_mode & 0o777 == 0o600
    assert _git_text(first.workspace, "rev-list", "--count", "HEAD") == "1"
    assert _git_text(first.workspace, "rev-parse", "HEAD") == repository.repository.source_revision
    assert _git_text(first.workspace, "rev-parse", "HEAD^{tree}") == repository.source_tree_oid
    assert first.manifest.baseline_tree_sha256 == repository.repository.baseline_tree_sha256

    changed = first.workspace / "src/mathbox.py"
    changed.write_text(changed.read_text(encoding="utf-8").replace("left - right", "left + right"))
    reopened = materialize_task_pack_case(
        loaded,
        runs,
        _git(),
        first.case.case_id,
        run_id,
    )
    assert reopened.workspace == first.workspace
    assert "left + right" in changed.read_text(encoding="utf-8")
    assert _git_text(first.workspace, "status", "--short") == "M src/mathbox.py"


def test_materializes_every_builtin_case_with_exact_license_and_tree(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack()
    runs = _runs_root(tmp_path)
    for case in loaded.manifest.cases:
        item = materialize_task_pack_case(loaded, runs, _git(), case.case_id, uuid4())
        repository = loaded.manifest.repository(case.repository_id)
        license_body = (item.workspace / repository.license.license_file).read_bytes()
        assert __import__("hashlib").sha256(license_body).hexdigest() == (
            repository.license.license_sha256
        )
        assert int(_git_text(item.workspace, "rev-list", "--count", "HEAD")) == 1
        assert item.manifest.tracked_files == repository.tracked_files
        assert item.manifest.source_archive_sha256 == repository.archive_sha256


def test_materialization_fails_closed_for_case_path_and_manifest_mismatch(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack()
    runs = _runs_root(tmp_path)
    with pytest.raises(KernelError) as missing:
        materialize_task_pack_case(loaded, runs, _git(), "missing", uuid4())
    assert missing.value.code == "eval_task_pack_case_not_found"

    run_id = uuid4()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (runs / str(run_id)).symlink_to(target, target_is_directory=True)
    with pytest.raises(KernelError) as symlink:
        materialize_task_pack_case(
            loaded,
            runs,
            _git(),
            "python-mathbox-addition",
            run_id,
        )
    assert symlink.value.code == "eval_task_pack_materialization_incomplete"

    run_id = uuid4()
    item = materialize_task_pack_case(
        loaded,
        runs,
        _git(),
        "python-mathbox-addition",
        run_id,
    )
    path = item.run_root / "materialization.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["case_id"] = "javascript-slug-lowercase"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(KernelError) as mismatch:
        load_materialized_task_pack_case(
            loaded,
            runs,
            _git(),
            "python-mathbox-addition",
            run_id,
        )
    assert mismatch.value.code == "eval_task_pack_materialization_mismatch"


def test_materialization_detects_changed_head(tmp_path: Path) -> None:
    item = _materialize(tmp_path, "javascript-slug-lowercase")
    target = item.workspace / "src/slug.mjs"
    target.write_text(target.read_text().replace("value.trim()", "value.trim().toLowerCase()"))
    subprocess.run((_git(), "add", "src/slug.mjs"), cwd=item.workspace, check=True)
    subprocess.run(
        (
            _git(),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "change",
        ),
        cwd=item.workspace,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    with pytest.raises(KernelError) as changed:
        load_materialized_task_pack_case(
            builtin_coding_eval_task_pack(),
            item.run_root.parent,
            _git(),
            item.case.case_id,
            item.manifest.run_id,
        )
    assert changed.value.code == "eval_task_pack_materialization_changed"


def test_profile_projection_is_exact_and_engine_is_validated(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack()
    source = loaded.manifest.profile("python-unittest")
    profile = build_task_pack_product_profile(loaded, source.profile_id, _git())

    assert profile.profile_id == source.profile_id
    assert profile.image == source.image
    assert profile.program == source.program
    assert profile.arguments == source.arguments
    assert profile.selector_policy == "none"
    assert profile.network_mode == "none"
    assert not profile.secret_refs
    assert profile.container_engine == str(_git().resolve())
    assert source.profile_sha256[:16] in profile.version

    with pytest.raises(KernelError) as missing:
        build_task_pack_product_profile(loaded, "missing", _git())
    assert missing.value.code == "eval_task_pack_profile_not_found"
    with pytest.raises(KernelError) as directory:
        build_task_pack_product_profile(loaded, source.profile_id, tmp_path)
    assert directory.value.code == "eval_task_pack_profile_invalid"
    non_executable = tmp_path / "engine"
    non_executable.write_text("engine", encoding="utf-8")
    non_executable.chmod(0o600)
    with pytest.raises(KernelError) as executable:
        build_task_pack_product_profile(loaded, source.profile_id, non_executable)
    assert executable.value.code == "eval_task_pack_profile_invalid"


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-task-pack", CodingEvalTaskPack),
        ("coding-eval-task-pack-materialization", CodingEvalTaskPackMaterialization),
        ("coding-eval-review-oracle", CodingEvalReviewOracle),
    ],
)
def test_task_pack_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    expected = json.loads(Path(f"spec/{name}-v1.schema.json").read_text(encoding="utf-8"))
    assert expected == model.model_json_schema()


def test_task_pack_resources_are_packaged_as_regular_files() -> None:
    root = resources.files("harnessix.evals").joinpath("taskpacks", "v1")
    assert root.joinpath("manifest.json").is_file()
    assert root.joinpath("archives", "javascript-slug.tar").is_file()
    assert root.joinpath("archives", "python-mathbox.tar").is_file()
    for version in (1, 2):
        engineering = resources.files("harnessix.evals").joinpath(
            "taskpacks", f"engineering-v{version}"
        )
        assert engineering.joinpath("manifest.json").is_file()
        assert engineering.joinpath("archives", "agents-utils-benchmark.tar").is_file()
        assert engineering.joinpath("archives", "langchain-utils-benchmark.tar").is_file()
        assert engineering.joinpath("archives", "opencode-utils-benchmark.tar").is_file()
