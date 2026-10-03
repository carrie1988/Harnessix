"""执行文件创建事件与既有断点合同；不把生成文本当Windows实际见证。"""

from __future__ import annotations

import re

import pytest

from scripts.windows_git_native_branch_observation.contract import read_contract
from scripts.windows_git_native_branch_observation.preflight import debugger_scripts
from scripts.windows_git_native_branch_observation.projection import branch_records


def generated(tmp_path):
    core = next(row for row in read_contract()["pairs"] if row["role"] == "core")
    debugger_scripts(tmp_path, core)
    return {p.name: p.read_text(encoding="ascii") for p in tmp_path.glob("*.cdb")}


def test_main_executable_arming_uses_one_filtered_child_creation_event(tmp_path):
    files = generated(tmp_path)
    bootstrap = files["bootstrap.cdb"]
    callback = str(tmp_path / "on-git-load.cdb")
    assert bootstrap.count(".childdbg 1") == 1
    assert bootstrap.count('sxe -c "') == 1
    assert f'sxe -c "$$><{callback}; g" cpr:git.exe\n' in bootstrap
    assert "ld:git.exe" not in bootstrap
    assert "sxi cpr" not in bootstrap
    assert bootstrap.endswith("g\n")


@pytest.mark.parametrize("name", ["on-fstat.cdb", "on-index.cdb", "on-git-load.cdb"])
def test_printf_retains_one_c_newline_escape_and_not_literal_backslash(tmp_path, name):
    text = generated(tmp_path)[name]
    formats = re.findall(r'\.printf "([^"]+)"', text)
    assert len(formats) == 1
    assert formats[0].endswith(chr(92) + "n")
    assert not formats[0].endswith(chr(92) * 2 + "n")
    assert "\n" not in formats[0]


def test_process_event_does_not_weaken_machine_guards_or_add_breakpoints(tmp_path):
    files = generated(tmp_path)
    loaded = files["on-git-load.cdb"]
    assert loaded.count("ba e 1") == 2
    assert "dwo(git+dwo(git+0x3c)+0x50) == 0x49d000" in loaded
    assert loaded.count("(dwo(git+0x") == 4
    assert loaded.count("(wo(git+0x") == 4
    assert loaded.count("(by(git+0x") == 4
    assert ".reload /f git.exe" in loaded
    for symbol, rva in (("hash_fd", "70b40"), ("mingw_fstat", "315ca0"), ("index_fd", "1e7fd0")):
        assert f"git!{symbol}-git == 0x{rva}" in loaded
    for name in ("on-fstat.cdb", "on-index.cdb"):
        assert "(@ebx == 0) && (@rdi == 0) && ((@esi & 1) != 0)" in files[name]
        assert files[name].endswith("gc\n")


@pytest.mark.parametrize("name", ["on-fstat.cdb", "on-index.cdb", "on-git-load.cdb"])
def test_script_templates_are_not_native_branch_witness(tmp_path, name):
    result = branch_records(generated(tmp_path)[name])
    assert result == {"two_material_invocations_witnessed": False, "invocations": []}
