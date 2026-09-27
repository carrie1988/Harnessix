"""固定规则扫描的漏扫负例；夹具只在内存构造，不保存真实凭据。"""

from __future__ import annotations

import bz2
import dataclasses
import gzip
import io
import lzma
import os
import re
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import secret_scan


def _canary() -> bytes:
    return b'api_key = "' + b"A" * 32 + b'"'


@pytest.mark.parametrize("kind", ["example", "binary", "wheel"])
def test_previously_skipped_content_is_scanned(tmp_path: Path, kind: str) -> None:
    path = tmp_path / {"example": ".env.example", "binary": "asset.bin", "wheel": "x.whl"}[kind]
    if kind == "wheel":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("package/config.py", _canary())
        path.write_bytes(buffer.getvalue())
    else:
        path.write_bytes((b"\x00" if kind == "binary" else b"") + _canary())
    findings = secret_scan.scan_paths([path])
    assert any(item["rule"] == "generic_api_assignment" for item in findings)


def test_missing_input_does_not_report_clean(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unreadable"):
        secret_scan.scan_paths([tmp_path / "missing.txt"])


def _zip(
    data: bytes, *, name: str = "package/config.py", method: int = zipfile.ZIP_DEFLATED
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as archive:
        archive.writestr(name, data)
    return buffer.getvalue()


def _tar(data: bytes, *, name: str = "package/config.py", kind: str = "file") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        if kind == "file":
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        else:
            entry.type = tarfile.SYMTYPE
            entry.linkname = "config.py"
            archive.addfile(entry)
    return buffer.getvalue()


def _scan(
    tmp_path: Path, body: bytes, suffix: str = ".bin", **limits: object
) -> list[dict[str, object]]:
    path = tmp_path / ("artifact" + suffix)
    path.write_bytes(body)
    return secret_scan.scan_paths(
        [path], limits=dataclasses.replace(secret_scan.ScanLimits(), **limits)
    )


@pytest.mark.parametrize(
    "format", ["zip", "stored", "tar", "tar.gz", "tar.bz2", "tar.xz", "renamed"]
)
def test_supported_archive_members_are_scanned(tmp_path: Path, format: str) -> None:
    tar = _tar(_canary())
    bodies = {
        "zip": _zip(_canary()),
        "stored": _zip(_canary(), method=zipfile.ZIP_STORED),
        "tar": tar,
        "tar.gz": gzip.compress(tar),
        "tar.bz2": bz2.compress(tar),
        "tar.xz": lzma.compress(tar),
        "renamed": _zip(_canary()),
    }
    suffix = ".bin" if format in ("stored", "renamed") else "." + format
    findings = _scan(tmp_path, bodies[format], suffix)
    assert any(
        item["rule"] == "generic_api_assignment" and "::member-" in str(item["path"])
        for item in findings
    )


@pytest.mark.parametrize(
    "body,suffix",
    [
        (_zip(b"public"), ".whl"),
        (_tar(b"public"), ".tar"),
        (_zip(b""), ".zip"),
        (_tar(b""), ".tar"),
    ],
)
def test_benign_archives_are_clean(tmp_path: Path, body: bytes, suffix: str) -> None:
    assert _scan(tmp_path, body, suffix) == []


def test_nested_archives_share_budget_and_are_scanned(tmp_path: Path) -> None:
    body = _zip(gzip.compress(_tar(_canary())), name="inner.tar.gz")
    assert _scan(tmp_path, body, ".whl")
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_depth_limit"):
        _scan(tmp_path, body, ".whl", archive_depth=2)


@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-32-le", b"\xff\xfe\x00\x00"),
        ("utf-32-be", b"\x00\x00\xfe\xff"),
    ],
)
def test_bom_text_is_scanned(tmp_path: Path, encoding: str, bom: bytes) -> None:
    assert _scan(tmp_path, bom + _canary().decode("ascii").encode(encoding))


def test_invalid_bom_text_blocks(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_text_encoding_invalid"):
        _scan(tmp_path, b"\xff\xfeA")


@pytest.mark.parametrize(
    "suffix,body,code",
    [
        (".whl", b"broken", "scan_archive_invalid"),
        (".tar", b"broken", "scan_archive_invalid"),
        (".gz", b"broken", "scan_archive_invalid"),
        (".xz", b"broken", "scan_archive_invalid"),
        (".bz2", b"broken", "scan_archive_invalid"),
        (".7z", b"public", "scan_archive_unsupported"),
        (".bin", b"Rar!bad", "scan_archive_unsupported"),
        (".zst", b"public", "scan_archive_unsupported"),
    ],
)
def test_corrupt_or_unsupported_archives_block(
    tmp_path: Path, suffix: str, body: bytes, code: str
) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match=code):
        _scan(tmp_path, body, suffix)


@pytest.mark.parametrize("format", ["gz", "bz2", "xz"])
def test_truncated_and_concatenated_compression_blocks(tmp_path: Path, format: str) -> None:
    compress = {"gz": gzip.compress, "bz2": bz2.compress, "xz": lzma.compress}[format]
    body = compress(b"public")
    for invalid in (body[:-2], body + compress(_canary())):
        with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_invalid"):
            _scan(tmp_path, invalid, "." + format)


@pytest.mark.parametrize("format", ["gz", "bz2", "xz"])
def test_compression_expansion_is_bounded(tmp_path: Path, format: str) -> None:
    compress = {"gz": gzip.compress, "bz2": bz2.compress, "xz": lzma.compress}[format]
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_byte_limit"):
        _scan(tmp_path, compress(b"A" * 1024 * 1024), "." + format, expanded_bytes=1024)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("file_bytes", 2, "scan_file_limit"),
        ("total_bytes", 2, "scan_byte_limit"),
        ("findings", 0, "scan_finding_limit"),
        ("entries", 0, "scan_entry_limit"),
        ("seconds", -1.0, "scan_timeout"),
    ],
)
def test_fixed_budgets_block_incomplete_scan(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match=code):
        _scan(tmp_path, _canary(), **{field: value})


def test_zip_declared_member_limit_blocks_before_object_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _zip(b"abc")

    def forbidden(*_: object, **__: object) -> None:
        pytest.fail("ZipFile必须在记录预算检查之后创建")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_member_limit"):
        _scan(tmp_path, body, ".zip", member_bytes=2)


@pytest.mark.parametrize("field,value", [("archive_entries", 0), ("central_directory_bytes", 1)])
def test_zip_metadata_limits_block(tmp_path: Path, field: str, value: int) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_limit"):
        _scan(tmp_path, _zip(b"public"), ".zip", **{field: value})


def test_forged_zip_entry_count_cannot_bypass_actual_metadata_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a", b"public")
        archive.writestr("b", b"public")
    body = bytearray(buffer.getvalue())
    end = body.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", body, end + 8, 1, 1)

    def forbidden(*_: object, **__: object) -> None:
        pytest.fail("伪造条目数不能进入ZipFile")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_limit"):
        _scan(tmp_path, bytes(body), ".zip", archive_entries=1)


@pytest.mark.parametrize(
    "mutation", ["encrypted", "zip64", "multidisk", "algorithm", "crc", "filename"]
)
def test_zip_unsupported_or_inconsistent_records_block(tmp_path: Path, mutation: str) -> None:
    body = bytearray(_zip(b"public", name="a"))
    central = body.index(b"PK\x01\x02")
    end = body.rfind(b"PK\x05\x06")
    expected = "scan_archive_unsupported"
    if mutation == "encrypted":
        struct.pack_into("<H", body, central + 8, 1)
    elif mutation == "zip64":
        struct.pack_into("<H", body, central + 6, 45)
    elif mutation == "multidisk":
        struct.pack_into("<H", body, end + 4, 1)
    elif mutation == "algorithm":
        struct.pack_into("<H", body, central + 10, 12)
    elif mutation == "crc":
        struct.pack_into("<I", body, central + 16, 0)
        struct.pack_into("<I", body, 14, 0)
        expected = "scan_archive_invalid"
    else:
        body[30] = ord("b")
        expected = "scan_archive_invalid"
    with pytest.raises(secret_scan.ScanIncompleteError, match=expected):
        _scan(tmp_path, bytes(body), ".zip")


def test_zip_compression_tail_cannot_hide_another_payload(tmp_path: Path) -> None:
    body = bytearray(_zip(b"public", name="a"))
    central = body.index(b"PK\x01\x02")
    payload = _zip(_canary())
    old_compressed = struct.unpack_from("<I", body, 18)[0]
    body[central:central] = payload
    central += len(payload)
    end = body.rfind(b"PK\x05\x06")
    struct.pack_into("<I", body, 18, old_compressed + len(payload))
    struct.pack_into("<I", body, central + 20, old_compressed + len(payload))
    struct.pack_into("<I", body, end + 16, central)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_invalid"):
        _scan(tmp_path, bytes(body), ".zip")


@pytest.mark.parametrize("name", ["../config.py", "/config.py", "C:/config.py", "dir\\config.py"])
@pytest.mark.parametrize("format", ["zip", "tar"])
def test_unsafe_archive_paths_block_without_extraction(
    tmp_path: Path, name: str, format: str
) -> None:
    body = _zip(b"public", name=name) if format == "zip" else _tar(b"public", name=name)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_entry_unsupported"):
        _scan(tmp_path, body, "." + format)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["artifact." + format]


def test_tar_link_metadata_limits_and_trailing_data_block(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_entry_unsupported"):
        _scan(tmp_path, _tar(b"", kind="link"), ".tar")
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_limit"):
        _scan(tmp_path, _tar(b"public"), ".tar", archive_entries=0)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_member_limit"):
        _scan(tmp_path, _tar(b"public"), ".tar", member_bytes=2)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_invalid"):
        _scan(tmp_path, _tar(b"public") + _zip(_canary()), ".tar")


@pytest.mark.parametrize("index", list(range(6)))
def test_self_check_detects_each_disabled_rule(monkeypatch: pytest.MonkeyPatch, index: int) -> None:
    assert secret_scan._self_check()
    rules = list(secret_scan.RULES)
    name, _ = rules[index]
    rules[index] = (name, re.compile(rb"(?!)"))
    monkeypatch.setattr(secret_scan, "RULES", tuple(rules))
    assert not secret_scan._self_check()


def test_read_failure_and_cancel_do_not_render_raw_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "public.txt"
    path.write_bytes(b"public")

    def broken(*_: object, **__: object) -> object:
        raise OSError(_canary().decode("ascii"))

    monkeypatch.setattr(os, "open", broken)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unreadable") as caught:
        secret_scan.scan_paths([path])
    assert _canary().decode("ascii") not in str(caught.value)

    def cancel(*_: object, **__: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "open", cancel)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_cancelled"):
        secret_scan.scan_paths([path])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=20)


@pytest.mark.parametrize("name", ["中文 文件.txt", "line\nbreak.txt"])
def test_git_listing_preserves_actual_filename(tmp_path: Path, name: str) -> None:
    if os.name == "nt" and "\n" in name:
        pytest.skip("Windows原生文件名不允许换行")
    path = tmp_path / name
    path.write_bytes(b"public")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "--", name)
    assert secret_scan._tracked_files(tmp_path) == [path]
    assert secret_scan.scan_paths(secret_scan._tracked_files(tmp_path)) == []


@pytest.mark.parametrize("mode", ["module", "script"])
def test_cli_findings_and_incomplete_status_are_utf8_and_sanitized(
    tmp_path: Path, mode: str
) -> None:
    _git(tmp_path, "init")
    path = tmp_path / "中文 文件.txt"
    path.write_bytes(_canary())
    _git(tmp_path, "add", "--", path.name)
    entry = ["-m", "scripts.secret_scan"] if mode == "module" else ["scripts/secret_scan.py"]
    root = Path(__file__).resolve().parents[2]

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *entry, "--root", str(tmp_path)],
            cwd=root,
            env={**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"},
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    result = run()
    assert result.returncode == 1 and "location-sha256:" in result.stderr
    assert _canary().decode("ascii") not in result.stderr
    assert str(path) not in result.stderr and "中文 文件.txt" not in result.stderr
    path.unlink()
    result = run()
    assert result.returncode == 2 and "scan_input_unreadable" in result.stderr
    assert "Traceback" not in result.stderr and str(path) not in result.stderr


def test_directory_discovery_error_and_links_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_walk(*_: object, onerror: object, **__: object) -> list[object]:
        onerror(OSError("private filesystem context"))
        return []

    monkeypatch.setattr(os, "walk", failed_walk)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_discovery_failed"):
        secret_scan._artifact_files(tmp_path)


def test_symlinks_never_silently_skip(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"public")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows链接测试需要开发者模式或创建符号链接权限")
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unsupported"):
        secret_scan.scan_paths([link])
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unsupported"):
        secret_scan._artifact_files(link)


def test_explicit_missing_artifact_directory_is_not_source_only_success(tmp_path: Path) -> None:
    assert secret_scan._artifact_files(tmp_path / "missing") == []
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unreadable"):
        secret_scan._artifact_files(tmp_path / "missing", required=True)


def test_empty_zip_and_empty_tar_are_supported(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    assert _scan(tmp_path, buffer.getvalue(), ".whl") == []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w"):
        pass
    assert _scan(tmp_path, buffer.getvalue(), ".tar") == []


def test_zip_data_descriptor_is_supported(tmp_path: Path) -> None:
    class StreamingOutput(io.BytesIO):
        def seekable(self) -> bool:
            return False

        def seek(self, *args: object, **kwargs: object) -> int:
            raise io.UnsupportedOperation

    buffer = StreamingOutput()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.py", _canary())
    assert b"PK\x07\x08" in buffer.getvalue()
    assert _scan(tmp_path, buffer.getvalue(), ".whl")


def test_orphan_zip_member_cannot_hide_compressed_secret(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a", _canary())
        archive.writestr("b", b"public")
    body = bytearray(buffer.getvalue())
    central = body.index(b"PK\x01\x02")
    first_record_size = 47
    del body[central : central + first_record_size]
    end = body.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", body, end + 8, 1, 1)
    struct.pack_into("<I", body, end + 12, first_record_size)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_invalid"):
        _scan(tmp_path, bytes(body), ".whl")


def test_file_change_during_scan_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "public.txt"
    path.write_bytes(b"public")
    original = os.fstat
    calls = 0

    def changed(descriptor: int) -> os.stat_result:
        nonlocal calls
        result = original(descriptor)
        calls += 1
        if calls == 2:
            path.write_bytes(b"changed")
            result = original(descriptor)
        return result

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_changed"):
        secret_scan.scan_paths([path])


@pytest.mark.skipif(os.name == "nt", reason="Windows没有POSIX FIFO输入")
def test_fifo_blocks_before_read(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_input_unsupported"):
        secret_scan.scan_paths([path])


def test_repository_discovery_failure_uses_fixed_error(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_discovery_failed"):
        secret_scan._tracked_files(tmp_path)


def test_gate_builds_real_artifacts_before_scanning() -> None:
    root = Path(__file__).resolve().parents[2]
    makefile = (
        (root / "Makefile")
        .read_text(encoding="utf-8")
        .split("supply-chain:\n", 1)[1]
        .split("\n\n", 1)[0]
    )
    assert makefile.index("uv build --offline") < makefile.index("--artifact-dir dist/secret-gate")
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for name in ("python", "coding-tools-macos", "windows-trusted-execution"):
        steps = workflow["jobs"][name]["steps"]
        commands = [step.get("run", "") for step in steps]
        build = commands.index("uv build --offline --out-dir dist/secret-gate")
        scan = commands.index(
            "uv run python scripts/secret_scan.py --artifact-dir dist/secret-gate"
        )
        assert build < scan and steps[scan]["timeout-minutes"] == 2


def test_explicit_empty_artifacts_block(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_artifact_missing"):
        secret_scan._artifact_files(tmp_path, required=True)


def test_scan_command_always_checks_rule_health(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(secret_scan, "RULES", ())
    assert secret_scan.main([]) == 2
    assert "固定规则正反例不完整" in capsys.readouterr().err


def test_tar_pax_metadata_size_is_bounded(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entry = tarfile.TarInfo("public.txt")
        entry.pax_headers = {"comment": "A" * 65536}
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_member_limit"):
        _scan(tmp_path, buffer.getvalue(), ".tar")


def test_zip_links_block(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo("link.txt")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, b"target.txt")
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_archive_entry_unsupported"):
        _scan(tmp_path, buffer.getvalue(), ".whl")


def test_archive_members_cannot_reset_total_entry_budget(tmp_path: Path) -> None:
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_entry_limit"):
        _scan(tmp_path, _zip(b"public"), ".whl", entries=1)


def test_files_cannot_reset_total_byte_budget(tmp_path: Path) -> None:
    paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for path in paths:
        path.write_bytes(b"public")
    with pytest.raises(secret_scan.ScanIncompleteError, match="scan_byte_limit"):
        secret_scan.scan_paths(
            paths, limits=dataclasses.replace(secret_scan.ScanLimits(), total_bytes=10)
        )
