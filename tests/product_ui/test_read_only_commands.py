"""完成证据里不算检查的命令（Beta Issue #36的后续）：判定不了的一律仍算检查。"""

from __future__ import annotations

import pytest

from harnessix.product_ui import read_only_commands
from harnessix.product_ui.read_only_commands import is_read_only_command


@pytest.mark.parametrize(
    ("shell", "command"),
    [
        ("posix", "ls -la src"),
        ("posix", "git status"),
        ("posix", "git diff -- src/a.py"),
        ("posix", "cat src/a.py; exit $?"),
        ("posix", "grep -rn needle src | head -20"),
        ("posix", "rg 'a|b' src | sort | uniq"),
        ("powershell", "Get-Content src\\a.py"),
        ("powershell", "Test-Path repro.test.ts"),
        ("powershell", "Select-String -Path a.txt -Pattern 'x|y' | Select-Object -First 3"),
        ("powershell", "sls needle a.txt | Measure-Object; exit $LASTEXITCODE"),
        ("powershell", "C:\\Windows\\System32\\findstr.exe /s needle *.ts"),
    ],
)
def test_read_only_and_search_commands(monkeypatch, shell: str, command: str) -> None:
    monkeypatch.setattr(read_only_commands.os, "name", "nt" if shell == "powershell" else "posix")
    assert is_read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "pnpm vitest run --root pkgs/core",
        "go test ./viewport/",
        "python tmp_probe.py 2>&1 | Select-String -NotMatch plugin",
        "grep x a.txt > out.txt",
        "grep x a.txt && npm test",
        "grep x a.txt || npm test",
        "grep x a.txt | xargs rm",
        "grep $(cat names) a.txt",
        "Remove-Item a.txt; Test-Path a.txt",
        "cat 'unterminated",
        "grep x |",
    ],
)
def test_anything_else_still_counts_as_a_check(command: str) -> None:
    assert not is_read_only_command(command)
