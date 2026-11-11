"""沙箱外命令的免询问判定（ADR 0128）：只读命令从严识别，绕过写法一律交给用户。"""

from __future__ import annotations

import pytest

from harnessix.product_config.command_rules import (
    read_only_paths,
    rule_label,
    rule_matches,
    rule_prefix,
    simple_command,
)


def _read_only(cmd: str, shell: str) -> tuple[str, ...] | None:
    words = simple_command(cmd, shell)  # type: ignore[arg-type]
    return None if words is None else read_only_paths(words, shell)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cmd", "paths"),
    [
        ("ls", ()),
        ("ls -la src", ("src",)),
        ("ls *.py", ("*.py",)),
        ("pwd", ()),
        ("cat README.md", ("README.md",)),
        ("cat ./src/app.py tests/test_app.py", ("src/app.py", "tests/test_app.py")),
        ("head -n 20 src/app.py", ("20", "src/app.py")),
        ("tail -5 'docs/user guide.md'", ("docs/user guide.md",)),
        ("wc -l src/app.py", ("src/app.py",)),
        ("diff a.txt b.txt", ("a.txt", "b.txt")),
        ("git status", ()),
        ("git diff --stat", ()),
        ("git log --oneline -n 5", ("5",)),
        ("git show HEAD:src/app.py", ("src/app.py",)),
        ("git diff HEAD~2..HEAD -- src", ("HEAD~2..HEAD", "src")),
    ],
)
def test_posix_read_only_commands_are_recognised(cmd: str, paths: tuple[str, ...]) -> None:
    assert _read_only(cmd, "posix") == paths


@pytest.mark.parametrize(
    "cmd",
    [
        # 复合、重定向、子表达式、变量、转义
        "ls; rm -rf .",
        "cat a.txt | sh",
        "ls && rm x",
        "cat a.txt > b.txt",
        "cat < a.txt",
        "ls $(rm x)",
        "ls `rm x`",
        "echo $HOME",
        "cat a{b,c}",
        "cat .en\\v",
        "ls\nrm x",
        # 工作区外或敏感文件
        "cat /etc/passwd",
        "cat ../other/secret.txt",
        "cat ~/.ssh/id_ed25519",
        "cat .env",
        "cat config/.env.local",
        "cat '.env'",
        "cat .e'n'v",
        "cat keys/server.pem",
        "cat .git/config",
        "cat .ENV",
        # 通配符会在Shell里展开成敏感文件
        "cat .e*",
        "cat .en?",
        "head -n 3 *",
        # 读文件内容的命令：带值的短选项、未知写法不放行
        "file -f.env",
        "wc --files0-from=.env",
        "diff --from-file=/etc/passwd a.txt",
        # 不在只读集合内
        "grep -r SECRET .",
        "find . -name x -delete",
        "rm x",
        "python -c 'print(1)'",
        "FOO=1 ls",
        "/bin/cat a.txt",
        # git：写文件、执行外部程序、读工作区外、全局选项、非只读子命令
        "git diff --output=/tmp/x",
        "git diff --output x",
        "git log --ext-diff",
        "git diff --no-index /etc/passwd a.txt",
        "git -c core.pager=sh log",
        "git -C /tmp status",
        "git show HEAD:.env",
        "git log -p -- .env",
        "git diff /etc/passwd",
        "git push",
        "git commit -m x",
        "git grep -Osh x",
        # 非ASCII：PowerShell把弯引号当引号，一律不免询问
        "cat “.env”",
        "cat 文档.md",
        "",
        "   ",
        "cat 'unterminated",
    ],
)
def test_posix_commands_that_are_not_provably_read_only_need_the_user(cmd: str) -> None:
    assert _read_only(cmd, "posix") is None


@pytest.mark.parametrize(
    ("cmd", "paths"),
    [
        ("Get-ChildItem", ()),
        ("Get-ChildItem -Recurse src", ("src",)),
        ("dir *.py", ("*.py",)),
        ("gci -Force", ()),
        ("Get-Location", ()),
        ("Get-Content README.md", ("README.md",)),
        ("get-content -Path .\\src\\app.py -TotalCount 20", ("src/app.py", "20")),
        ("type 'docs\\user guide.md'", ("docs/user guide.md",)),
        ("Test-Path src\\app.py", ("src/app.py",)),
        ("Get-FileHash dist\\app.zip", ("dist/app.zip",)),
        ("git.exe status", ()),
        ("GIT diff --stat", ()),
        ("git show HEAD:src/app.py", ("src/app.py",)),
    ],
)
def test_powershell_read_only_commands_are_recognised(cmd: str, paths: tuple[str, ...]) -> None:
    assert _read_only(cmd, "powershell") == paths


@pytest.mark.parametrize(
    "cmd",
    [
        "Get-Content a.txt | Invoke-Expression",
        "Get-Content a.txt; Remove-Item a.txt",
        "Get-Content (Remove-Item a.txt)",
        "Get-ChildItem | ForEach-Object { Remove-Item $_ }",
        "Get-Content a.txt > b.txt",
        "Get-Content $env:USERPROFILE\\.ssh\\id_rsa",
        "Get-Content `.env",
        # 工作区外、驱动器与提供程序路径、UNC
        "Get-Content C:\\Users\\me\\.ssh\\id_ed25519",
        "Get-Content C:secret.txt",
        "Get-Content \\\\server\\share\\x.txt",
        "Get-Content ..\\other\\x.txt",
        "Get-ChildItem Env:",
        "Get-Item HKLM:\\Software",
        "Get-Content ~\\.ssh\\id_rsa",
        # 敏感文件及其Windows别名写法
        "Get-Content .env",
        "Get-Content .\\.env",
        "Get-Content -Path .ENV",
        "Get-Content '.env'",
        "Get-Content keys\\server.pem",
        "Get-Content id_rsa.",
        "Get-Content 'id_rsa '",
        "Get-Content ENV~1",
        "Get-Content a.txt,.env",
        "Get-Content -LiteralPath:.env",
        "Get-Content a.txt:secret",
        "Get-Content @args",
        # 通配符
        "Get-Content .e*",
        "Get-Content [.]env",
        # 不在只读集合内
        "Select-String -Path . -Pattern SECRET",
        "Remove-Item a.txt",
        "Invoke-Expression 'ls'",
        ".\\tool.exe",
        "git diff C:\\Users\\me\\secret.txt",
        "git show HEAD:.env",
        "Get-Content “.env”",
    ],
)
def test_powershell_commands_that_are_not_provably_read_only_need_the_user(cmd: str) -> None:
    assert _read_only(cmd, "powershell") is None


@pytest.mark.parametrize(
    ("cmd", "shell", "label"),
    [
        ("python -m unittest -v", "posix", "python -m unittest *"),
        ("python3 -m pytest tests/test_a.py", "posix", "python3 -m pytest *"),
        ("uv run pytest -q", "posix", "uv run pytest *"),
        ("npm run build", "posix", "npm run build *"),
        ("npm test", "posix", "npm test *"),
        ("git push origin main", "posix", "git push *"),
        ("pytest -q tests", "posix", "pytest *"),
        ("python manage.py test", "posix", "python *"),
        ("uv --version", "posix", "uv *"),
        ("Python.exe -m unittest", "powershell", "python -m unittest *"),
        ("cargo test --all", "powershell", "cargo test *"),
    ],
)
def test_the_remembered_rule_is_a_command_prefix(cmd: str, shell: str, label: str) -> None:
    words = simple_command(cmd, shell)  # type: ignore[arg-type]
    assert words is not None
    prefix = rule_prefix(words, shell)  # type: ignore[arg-type]
    assert prefix is not None and rule_label(prefix) == label


@pytest.mark.parametrize(
    ("cmd", "shell"),
    [
        ("rm -rf build", "posix"),
        ("sudo make install", "posix"),
        ("bash -c 'make'", "posix"),
        ("python -c 'import os'", "posix"),
        ("node -e 'x'", "posix"),
        ("./deploy.sh prod", "posix"),
        ("env FOO=1 make", "posix"),
        ("Remove-Item build -Recurse", "powershell"),
        ("powershell.exe -Command ls", "powershell"),
        ("Invoke-Expression 'x'", "powershell"),
        (".\\deploy.ps1", "powershell"),
    ],
)
def test_destructive_and_inline_code_commands_are_never_remembered(cmd: str, shell: str) -> None:
    words = simple_command(cmd, shell)  # type: ignore[arg-type]
    assert words is not None
    assert rule_prefix(words, shell) is None  # type: ignore[arg-type]


def test_a_saved_rule_only_matches_simple_commands_with_that_prefix() -> None:
    prefix = ("python", "-m", "unittest")

    def matches(cmd: str, shell: str = "posix") -> bool:
        words = simple_command(cmd, shell)  # type: ignore[arg-type]
        return words is not None and rule_matches(prefix, words, shell)  # type: ignore[arg-type]

    assert matches("python -m unittest")
    assert matches("python -m unittest discover -s tests -v")
    assert not matches("python -m pytest")
    assert not matches("python -m")
    assert not matches("python3 -m unittest")
    # 复合命令永远不匹配：后半段可以是任何命令。
    assert not matches("python -m unittest; rm -rf .")
    assert not matches("python -m unittest && curl example.com")
    assert not matches("Python -m unittest")
    assert matches("Python.exe -m unittest -v", "powershell")
    assert not matches("python -m unittest | Out-File x", "powershell")
