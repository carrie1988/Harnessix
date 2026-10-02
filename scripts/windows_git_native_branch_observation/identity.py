"""只读核对发行 PE、PDB 与两处分支机器码，不加载或运行 Git。"""

from __future__ import annotations

import hashlib
import struct
import uuid
from pathlib import Path


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def pdb_streams(body: bytes) -> list[bytes | None]:
    if body[:32] != b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0":
        raise ValueError("pdb_msf_signature_invalid")
    block_size, _, blocks, directory_size, _, map_block = struct.unpack_from("<6I", body, 32)
    if block_size not in (512, 1024, 2048, 4096) or blocks * block_size != len(body):
        raise ValueError("pdb_msf_bounds_invalid")
    count = (directory_size + block_size - 1) // block_size
    indices = struct.unpack_from("<" + "I" * count, body, map_block * block_size)
    directory = b"".join(body[i * block_size : (i + 1) * block_size] for i in indices)
    directory = directory[:directory_size]
    stream_count = struct.unpack_from("<I", directory)[0]
    sizes = struct.unpack_from("<" + "I" * stream_count, directory, 4)
    cursor = 4 + stream_count * 4
    result: list[bytes | None] = []
    for size in sizes:
        if size == 0xFFFFFFFF:
            result.append(None)
            continue
        count = (size + block_size - 1) // block_size
        indices = struct.unpack_from("<" + "I" * count, directory, cursor)
        cursor += count * 4
        if any(i >= blocks for i in indices):
            raise ValueError("pdb_stream_block_invalid")
        result.append(b"".join(body[i * block_size : (i + 1) * block_size] for i in indices)[:size])
    return result


def pdb_identity(path: Path) -> dict:
    body = path.read_bytes()
    streams = pdb_streams(body)
    info = streams[1]
    if info is None or len(info) < 28:
        raise ValueError("pdb_info_missing")
    _, _, age = struct.unpack_from("<III", info)
    result = {
        "sha256": sha256(body),
        "guid": str(uuid.UUID(bytes_le=info[12:28])).upper(),
        "age": age,
    }
    dbi = streams[3]
    if dbi is None:
        raise ValueError("pdb_dbi_missing")
    symbol_stream = struct.unpack_from("<H", dbi, 20)[0]
    symbols = streams[symbol_stream]
    if symbols is None:
        raise ValueError("pdb_symbols_missing")
    cursor = 0
    names = {}
    wanted = {"hash_fd", "mingw_fstat", "index_fd"}
    while cursor + 4 <= len(symbols):
        size, kind = struct.unpack_from("<HH", symbols, cursor)
        if size < 2 or cursor + size + 2 > len(symbols):
            raise ValueError("pdb_symbol_bounds_invalid")
        record = symbols[cursor + 4 : cursor + size + 2]
        if kind == 0x110E and len(record) >= 11:
            name = record[10:].split(b"\0", 1)[0].decode("utf-8", errors="strict")
            if name in wanted:
                offset, section = struct.unpack_from("<IH", record, 4)
                if name in names:
                    raise ValueError("pdb_branch_symbol_duplicate")
                names[name] = {"section": section, "section_offset": offset}
        cursor += size + 2
    result["branch_symbols"] = names
    return result


def pe_identity(path: Path) -> dict:
    body = path.read_bytes()
    if body[:2] != b"MZ":
        raise ValueError("pe_dos_signature_invalid")
    nt = struct.unpack_from("<I", body, 0x3C)[0]
    if body[nt : nt + 4] != b"PE\0\0":
        raise ValueError("pe_nt_signature_invalid")
    machine, count = struct.unpack_from("<HH", body, nt + 4)
    optional_size = struct.unpack_from("<H", body, nt + 20)[0]
    optional = nt + 24
    if machine != 0x8664 or struct.unpack_from("<H", body, optional)[0] != 0x20B:
        raise ValueError("pe_x64_required")
    sections = []
    for i in range(count):
        offset = optional + optional_size + i * 40
        virtual_size, rva, raw_size, raw = struct.unpack_from("<IIII", body, offset + 8)
        sections.append(
            {"rva": rva, "virtual_size": virtual_size, "raw_size": raw_size, "raw_offset": raw}
        )

    def file_offset(rva: int) -> int:
        for section in sections:
            delta = rva - section["rva"]
            if 0 <= delta < section["raw_size"]:
                return section["raw_offset"] + delta
        raise ValueError("pe_rva_unmapped")

    debug_rva, debug_size = struct.unpack_from("<II", body, optional + 112 + 6 * 8)
    codeview = []
    for i in range(debug_size // 28):
        offset = file_offset(debug_rva) + i * 28
        kind, size, _, raw = struct.unpack_from("<IIII", body, offset + 12)
        if kind == 2 and body[raw : raw + 4] == b"RSDS":
            codeview.append(
                {
                    "guid": str(uuid.UUID(bytes_le=body[raw + 4 : raw + 20])).upper(),
                    "age": struct.unpack_from("<I", body, raw + 20)[0],
                }
            )
    return {
        "sha256": sha256(body),
        "machine": "x64",
        "size_of_image": struct.unpack_from("<I", body, optional + 56)[0],
        "codeview": codeview,
        "sections": sections,
        "branch_bytes": {
            hex(rva): body[file_offset(rva) : file_offset(rva) + 7].hex()
            for rva in (0x70B6F, 0x70B74, 0x70BBC, 0x70BC1)
        }
        if len(body) > 0x100000
        else {},
    }


def check_pair(executable: Path, symbols: Path, expected: dict) -> dict:
    for path, prefix in ((executable, "pe"), (symbols, "pdb")):
        if path.stat().st_size != expected[prefix + "_bytes"]:
            raise ValueError("official_pair_size_mismatch")
        if sha256(path.read_bytes()) != expected[prefix + "_sha256"]:
            raise ValueError("official_pair_sha_mismatch")
    pe = pe_identity(executable)
    pdb = pdb_identity(symbols)
    if pe["size_of_image"] != expected["size_of_image"]:
        raise ValueError("official_pair_image_size_mismatch")
    if pe["sha256"] != expected["pe_sha256"] or pdb["sha256"] != expected["pdb_sha256"]:
        raise ValueError("official_pair_sha_mismatch")
    if not any(
        c["guid"] == pdb["guid"] == expected["guid"] and c["age"] == pdb["age"] == expected["age"]
        for c in pe["codeview"]
    ):
        raise ValueError("official_pair_guid_age_mismatch")
    if expected["role"] == "core":
        for name, rva in expected["symbol_rvas"].items():
            symbol = pdb["branch_symbols"][name]
            section = pe["sections"][symbol["section"] - 1]
            if section["rva"] + symbol["section_offset"] != int(rva, 16):
                raise ValueError("official_branch_symbol_rva_mismatch")
        for rva, machine_bytes in expected["branch_bytes"].items():
            if pe["branch_bytes"][rva] != machine_bytes:
                raise ValueError("official_branch_machine_bytes_mismatch")
    return {"role": expected["role"], "pe": pe, "pdb": pdb, "matched": True}
