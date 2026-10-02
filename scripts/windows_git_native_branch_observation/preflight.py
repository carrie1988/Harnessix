"""复用固定PE/PDB规则和两处硬件执行断点；只准备、不启动Windows进程。"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

from scripts.windows_git_native_branch_observation.contract import download_symbols, source_checks
from scripts.windows_git_native_branch_observation.identity import check_pair, sha256


def debugger_scripts(output: Path, core: dict) -> None:
    predicate = "(@ebx == 0) && (@rdi == 0) && ((@esi & 1) != 0)"
    breaks = []
    for rva, phase in (("70b74", "FSTAT"), ("70bc1", "INDEX")):
        handler = output / ("on-" + phase.lower() + ".cdb")
        handler.write_text(
            ".if ("
            + predicate
            + ') { .printf "FHX_NATIVE_BRANCH pid=%u tid=%u phase='
            + phase
            + ' rva=%I64x fd=%d path=%I64x flags=%x ret=%d\\n", '
            "@$tpid, @$tid, @rip-git, @ebx, @rdi, @esi, @eax; }\ngc\n",
            encoding="ascii",
        )
        breaks.append(f'ba e 1 git+0x{rva} "$$><{handler}"')
    # 先区分同名wrapper，不能在小镜像中直接解引用核心RVA。
    size_guard = f"dwo(git+dwo(git+0x3c)+0x50) == 0x{core['size_of_image']:x}"
    guards = []
    for rva, body in core["branch_bytes"].items():
        machine_bytes = bytes.fromhex(body)
        for offset, width, reader in ((0, 4, "dwo"), (4, 2, "wo"), (6, 1, "by")):
            value = int.from_bytes(machine_bytes[offset : offset + width], "little")
            guards.append(f"({reader}(git+{rva}+0x{offset:x}) == 0x{value:x})")
    code_guard = " && ".join(guards)
    symbol_guard = " && ".join(
        f"(git!{name}-git == {rva})" for name, rva in core["symbol_rvas"].items()
    )
    armed = '.printf "FHX_NATIVE_ARM pid=%u\\n", @$tpid'
    (output / "on-git-load.cdb").write_text(
        f".if ({size_guard}) {{ .if ({code_guard}) {{ .reload /f git.exe; "
        f".if ({symbol_guard}) {{ " + "; ".join([*breaks, armed]) + "; } } }\n",
        encoding="ascii",
    )
    (output / "bootstrap.cdb").write_text(
        ".symopt+0x400\n.symopt-0x40\n.childdbg 1\nsxi ibp\nsxi cpr\nsxi epr\n"
        + 'sxe -c "$$><'
        + str(output / "on-git-load.cdb")
        + '; g" ld:git.exe\ng\n',
        encoding="ascii",
    )


def selected_paths() -> tuple[Path, dict[str, Path]]:
    selected_string = shutil.which("git")
    if selected_string is None:
        raise ValueError("selected_git_missing")
    selected = Path(selected_string).resolve(strict=True)
    if selected.parent.name.lower() == "cmd":
        root = selected.parent.parent
    elif selected.parent.name.lower() == "bin" and selected.parent.parent.name.lower() == "mingw64":
        root = selected.parent.parent.parent
    else:
        raise ValueError("selected_git_layout_unrecognized")
    paths = {"wrapper": root / "cmd/git.exe", "core": root / "mingw64/bin/git.exe"}
    if selected not in {path.resolve(strict=True) for path in paths.values()}:
        raise ValueError("selected_git_not_verified_role")
    return selected, paths


def existing_tools(repository: Path) -> tuple[Path, Path]:
    cdb = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / (
        "Windows Kits/10/Debuggers/x64/cdb.exe"
    )
    python = repository / ".venv/Scripts/python.exe"
    if not cdb.is_file() or not python.is_file():
        raise ValueError("existing_tools_missing_no_install_performed")
    # CDB自身必须是现场已有的x64 PE；不下载或安装替代调试器。
    body = cdb.read_bytes()
    nt = struct.unpack_from("<I", body, 0x3C)[0]
    if body[:2] != b"MZ" or body[nt : nt + 4] != b"PE\0\0":
        raise ValueError("existing_cdb_pe_invalid")
    if struct.unpack_from("<H", body, nt + 4)[0] != 0x8664:
        raise ValueError("existing_cdb_x64_required")
    return cdb, python


def recheck(repository: Path, state: dict, contract: dict) -> None:
    source_checks(repository, contract)
    selected, paths = selected_paths()
    if selected != state["selected"] or paths != state["git_paths"]:
        raise ValueError("selected_git_changed")
    for row in contract["pairs"]:
        check_pair(paths[row["role"]], state["pdb_paths"][row["role"]], row)
    for tool in ("cdb", "python"):
        if sha256(state[tool].read_bytes()) != state[tool + "_sha256"]:
            raise ValueError("existing_tool_changed")


def prepare(repository: Path, output: Path, contract: dict) -> dict:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise ValueError("windows_x64_required")
    repository = repository.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(repository) or any(c in str(output) for c in ' \t\n\r";'):
        raise ValueError("private_nonrepository_simple_output_path_required")
    if output.exists():
        raise ValueError("private_output_preexists")
    sources = source_checks(repository, contract)
    selected, paths = selected_paths()
    cdb, python = existing_tools(repository)
    output.mkdir(parents=True)
    pdb_paths = download_symbols(output, contract)
    for row in contract["pairs"]:
        check_pair(paths[row["role"]], pdb_paths[row["role"]], row)
    debugger_scripts(output, next(row for row in contract["pairs"] if row["role"] == "core"))
    entry = Path(__file__).with_name("run_cases.py")
    return {
        "source_checks": sources,
        "selected": selected,
        "git_paths": paths,
        "pdb_paths": pdb_paths,
        "cdb": cdb,
        "python": python,
        "cdb_sha256": sha256(cdb.read_bytes()),
        "python_sha256": sha256(python.read_bytes()),
        "command": [
            str(cdb),
            "-o",
            "-G",
            "-hd",
            "-sins",
            "-netsyms:no",
            "-failinc",
            "-lines",
            "-noshell",
            "-nosqm",
            "-y",
            ";".join(str(path.parent) for path in pdb_paths.values()),
            "-cf",
            str(output / "bootstrap.cdb"),
            "-logo",
            str(output / "cdb-private.log"),
            str(python),
            str(entry),
            str(output),
        ],
    }
