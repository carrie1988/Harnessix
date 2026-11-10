"""产品CLI在旧代码页的重定向输出下不因中文崩溃（Windows冒烟发现）。"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from harnessix.cli import _use_utf8_output


def _run_with_cp1252_output(*arguments: str) -> subprocess.CompletedProcess[str]:
    # 模拟Windows把重定向输出按cp1252编码的环境。
    return subprocess.run(
        [sys.executable, "-m", "harnessix", *arguments],
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"},
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [(("--help",), "启动全屏Coding Agent终端产品"), (("license",), "AGPL-3.0-only")],
)
def test_chinese_output_survives_a_legacy_code_page(
    arguments: tuple[str, ...], expected: str
) -> None:
    result = _run_with_cp1252_output(*arguments)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "UnicodeEncodeError" not in result.stderr


def test_usage_errors_reach_stderr_in_utf8() -> None:
    result = _run_with_cp1252_output("没有这个命令")
    assert result.returncode == 2
    assert "没有这个命令" in result.stderr
    assert "UnicodeEncodeError" not in result.stderr


def test_streams_without_reconfigure_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    _use_utf8_output()
    assert sys.stdout is stdout and sys.stderr is stderr
