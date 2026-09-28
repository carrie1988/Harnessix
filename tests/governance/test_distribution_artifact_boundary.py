"""实际Hatch源码制品不递归携带验证制品，仓库扫描仍覆盖原始证据。"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path
from zipfile import ZipFile

from scripts.secret_scan import _tracked_files, scan_paths

ROOT = Path(__file__).parents[2]


def test_sdist_excludes_validation_artifacts_without_excluding_source_scan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copyfile(ROOT / name, project / name)
    source = project / "src/harnessix/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('"""固定源码夹具。"""\n', encoding="utf-8")
    installation = project / "docs/operations/installation.md"
    installation.parent.mkdir(parents=True)
    installation.write_text("# 正式安装文档夹具\n", encoding="utf-8")
    evidence = project / "docs/validation/fixture"
    evidence.mkdir(parents=True)
    (evidence / "README.md").write_text("# 验证证据夹具\n", encoding="utf-8")
    archive = evidence / "artifacts/nested.whl"
    archive.parent.mkdir()
    with ZipFile(archive, "w") as wheel:
        wheel.writestr("fixture.txt", b"Bearer " + b"A" * 32)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)

    uv = shutil.which("uv")
    assert uv is not None, "正式离线构建回归要求已有uv"
    output = tmp_path / "output"
    subprocess.run(
        [uv, "build", "--offline", "--sdist", "--out-dir", str(output)],
        cwd=project,
        check=True,
        capture_output=True,
        timeout=120,
    )
    distributions = list(output.glob("*.tar.gz"))
    assert len(distributions) == 1
    with tarfile.open(distributions[0], "r:gz") as distribution:
        names = {name.partition("/")[2] for name in distribution.getnames()}
    assert source.relative_to(project).as_posix() in names
    assert installation.relative_to(project).as_posix() in names
    assert (evidence / "README.md").relative_to(project).as_posix() in names
    assert not any(name.startswith("docs/validation/fixture/artifacts/") for name in names)
    assert scan_paths(distributions) == []
    # 发行物排除不是扫描白名单：Git中的同一原件仍需完整扫描并捕获合成命中。
    assert archive in _tracked_files(project)
    findings = scan_paths([archive])
    assert {finding["rule"] for finding in findings} == {"bearer_literal"}
    assert any("::member-" in str(finding["path"]) for finding in findings)
