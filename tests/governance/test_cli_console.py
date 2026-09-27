"""真实子进程验证旧代码页管道不会使治理CLI中文输出失败。"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.cli_console import configure_utf8_console

ROOT = Path(__file__).resolve().parents[2]


def _cp1252_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"},
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("mode", ["module", "script"])
@pytest.mark.parametrize(
    ("name", "arguments", "message", "returncode"),
    [
        ("sbom_generate", ["--check"], "SBOM一致且Schema有效", 0),
        ("license_scan", ["--check"], "许可证违规", 1),
        ("secret_scan", ["--self-check"], "Secret扫描自检通过", 0),
        ("documentation_check", ["--help"], "门禁", 0),
        ("generate_specs", ["--help"], "usage:", 0),
    ],
)
def test_cli_succeeds_with_cp1252_output_environment(
    name: str, arguments: list[str], message: str, returncode: int, mode: str
) -> None:
    entry = ["-m", f"scripts.{name}"] if mode == "module" else [f"scripts/{name}.py"]
    result = _cp1252_run([*entry, *arguments])
    assert result.returncode == returncode, result.stderr
    assert message in (result.stdout if returncode == 0 else result.stderr)
    assert "UnicodeEncodeError" not in result.stderr


def test_sbom_drift_has_utf8_diagnostic_and_original_failure_code(tmp_path: Path) -> None:
    missing = tmp_path / "不存在的库存.json"
    result = _cp1252_run(["-m", "scripts.sbom_generate", "--check", "--output", str(missing)])
    assert result.returncode == 1
    assert "缺失或漂移" in result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert not missing.exists()


def test_console_does_not_replace_embedded_capture_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    configure_utf8_console()
    assert sys.stdout is stdout and sys.stderr is stderr
    print("中文标准输出")
    print("中文标准错误", file=sys.stderr)
    assert stdout.getvalue() == "中文标准输出\n"
    assert stderr.getvalue() == "中文标准错误\n"
