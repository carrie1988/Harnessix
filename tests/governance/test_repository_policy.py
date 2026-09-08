from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGPL_SHA256 = "d8a6cc31abc16b6748c7a21f21611f5a1ec33f67d22ca23d7da1c19b95496bee"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_repository_declares_canonical_agpl_only() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["license"] == "AGPL-3.0-only"
    assert project["license-files"] == ["LICENSE"]
    assert "LICENSE text eol=lf" in _read(".gitattributes").splitlines()

    license_bytes = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == AGPL_SHA256
    license_text = license_bytes.decode("utf-8")
    assert license_text.startswith("GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3")
    assert "13. Remote Network Interaction" in license_text


def test_dual_license_governance_files_are_consistent() -> None:
    copyright_text = _read("COPYRIGHT.md")
    commercial_text = _read("COMMERCIAL_LICENSE.md")
    contributing_text = _read("CONTRIBUTING.md")
    readme = _read("README.md")

    assert "Copyright © 2026 Zhang Jinhui" in copyright_text
    assert "历史版本" in copyright_text and "MIT" in copyright_text
    assert "不授予任何商业许可证" in commercial_text
    assert "AGPL-3.0-only" in contributing_text
    assert "Signed-off-by:" in contributing_text
    assert "AGPL-3.0-only" in readme


def test_brand_and_third_party_boundaries_are_documented() -> None:
    trademarks = _read("TRADEMARKS.md")
    notices = _read("THIRD_PARTY_NOTICES.md")

    assert "代码许可证不授予任何商标" in trademarks
    assert "修改后的独立发行物" in trademarks
    assert "直接运行依赖" in notices
    assert "传递依赖许可证报告" in notices
