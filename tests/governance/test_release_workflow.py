"""发布工作流：前置检查逻辑，以及工作流的触发、权限和依赖形状。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from scripts import release_check
from scripts.release_check import ReleaseCheckError, checked_version, release_notes

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = """# 更新记录

## 0.10.0b2（2026-11-20）

### 新增

- 新功能

## 0.10.0b1（2026-11-12）

- 旧条目
"""


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))


def test_tag_must_be_v_plus_the_canonical_version() -> None:
    assert checked_version("v0.10.0b1", "0.10.0b1").is_prerelease
    assert not checked_version("v1.0.0", "1.0.0").is_prerelease
    for tag, version in (("0.10.0b1", "0.10.0b1"), ("v0.10.0b2", "0.10.0b1")):
        with pytest.raises(ReleaseCheckError, match="不一致"):
            checked_version(tag, version)
    with pytest.raises(ReleaseCheckError, match="规范形式"):
        checked_version("v0.10.0-beta1", "0.10.0-beta1")
    with pytest.raises(ReleaseCheckError, match="PEP 440"):
        checked_version("vlatest", "latest")


def test_release_notes_take_only_this_version() -> None:
    notes = release_notes(CHANGELOG, "0.10.0b2")
    assert notes == "### 新增\n\n- 新功能\n"
    assert release_notes(CHANGELOG, "0.10.0b1") == "- 旧条目\n"


@pytest.mark.parametrize(
    ("changelog", "message"),
    [
        (CHANGELOG, "缺少"),
        ("## 0.10.0b3（预览版，未发布）\n\n- 条目\n", "未发布"),
        ("## 0.10.0b3（2026-11-30）\n\n## 0.10.0b2\n", "为空"),
    ],
)
def test_release_notes_refuse_missing_unreleased_or_empty_sections(
    changelog: str, message: str
) -> None:
    with pytest.raises(ReleaseCheckError, match=message):
        release_notes(changelog, "0.10.0b3")


def test_gate_reports_version_and_prerelease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.10.0b2"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    output = tmp_path / "github-output"
    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_check.main(["gate", "--tag", "v0.10.0b2"]) == 0
    assert output.read_text(encoding="utf-8") == "version=0.10.0b2\nprerelease=true\n"
    assert release_check.main(["gate", "--tag", "v0.10.0b1"]) == 1
    assert "::error::" in capsys.readouterr().err


def test_wheel_check_ignores_hidden_build_files_and_records_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    # uv build会在输出目录写入.gitignore；0.10.0b1的发布因把它算作多余文件而中止。
    (dist / ".gitignore").write_text("*\n", encoding="utf-8")
    (dist / "harnessix-0.10.0b2-py3-none-any.whl").write_bytes(b"wheel")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_check.main(["wheel", "--dist", str(dist), "--version", "0.10.0b2"]) == 0
    assert output.read_text(encoding="utf-8") == f"sha256={hashlib.sha256(b'wheel').hexdigest()}\n"
    assert release_check.main(["wheel", "--dist", str(dist), "--version", "0.10.0b3"]) == 1
    (dist / "harnessix-0.10.0b2.tar.gz").write_bytes(b"sdist")
    with pytest.raises(ReleaseCheckError, match="应只有"):
        release_check.release_wheel(dist, "0.10.0b2")


def test_version_tags_do_not_trigger_another_ci_run() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert ci[True]["push"] == {"branches": ["**"]}


def test_release_runs_only_for_version_tags_with_read_only_default() -> None:
    workflow = _workflow()
    # PyYAML把键on解析为True。
    assert workflow[True] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_only_the_publish_job_can_mint_a_pypi_identity() -> None:
    jobs = _workflow()["jobs"]
    holders = [name for name, job in jobs.items() if "id-token" in job.get("permissions", {})]
    assert holders == ["publish"]
    publish = jobs["publish"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["needs"] == ["gate", "build", "smoke"]
    upload = publish["steps"][-1]
    assert upload["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert upload["with"] == {"packages-dir": "dist/"}


def test_release_waits_for_ci_mainline_and_three_platform_smoke() -> None:
    jobs = _workflow()["jobs"]
    gate = "\n".join(step.get("run", "") for step in jobs["gate"]["steps"])
    assert "release_check.py gate" in gate
    assert "merge-base --is-ancestor" in gate and "origin/main" in gate
    assert "workflows/ci.yml/runs?head_sha=" in gate and "/jobs?per_page=100" in gate
    # 只有Windows完整测试集不阻挡预览版（W1）；其余任务必须全部成功。
    assert 'select(.name != "windows-trusted-execution")' in gate
    assert 'select(.conclusion != "success")' in gate
    # 不要求整轮运行结束：Windows完整测试集可能仍在运行或挂起。
    assert "status=completed" not in gate
    assert '[.jobs[] | select(.name != "windows-trusted-execution")]' in gate
    assert jobs["gate"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["smoke"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    ]
    build = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert "secret_scan.py --artifact-dir dist" in build
    assert 'release_check.py wheel --dist dist --version "$VERSION"' in build
    release = jobs["github-release"]
    assert release["needs"] == ["gate", "build", "publish"]
    assert release["permissions"] == {"contents": "write"}
