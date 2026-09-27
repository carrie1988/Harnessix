"""不落盘解包的有界发行物读取；未支持的压缩形式必须失败关闭。"""

from __future__ import annotations

import bz2
import io
import lzma
import stat
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import PurePosixPath

if __package__:
    from scripts.secret_scan_contracts import ScanBudget, ScanIncompleteError
else:
    from secret_scan_contracts import ScanBudget, ScanIncompleteError


_ZIP_END = struct.Struct("<4s4H2IH")
_ZIP_ENTRY = struct.Struct("<4s6H3I5H2I")
_ZIP_LOCAL = struct.Struct("<4s5H3I2H")
_SUPPORTED_ZIP = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_UNSUPPORTED_SUFFIXES = (".7z", ".rar", ".zst", ".zstd", ".br")


def archive_kind(body: bytes, name: str) -> str | None:
    """后缀识别损坏产物，魔数识别改名产物；二者都不是文件豁免条件。"""

    lower = name.lower()
    if body.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\x28\xb5\x2f\xfd")) or lower.endswith(
        _UNSUPPORTED_SUFFIXES
    ):
        raise ScanIncompleteError("scan_archive_unsupported")
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06")) or lower.endswith(
        (".zip", ".whl", ".egg", ".jar")
    ):
        return "zip"
    if body.startswith(b"\x1f\x8b") or lower.endswith((".gz", ".tgz")):
        return "gz"
    if body.startswith(b"BZh") or lower.endswith((".bz2", ".tbz2")):
        return "bz2"
    if body.startswith(b"\xfd7zXZ\x00") or lower.endswith((".xz", ".txz")):
        return "xz"
    if body[257:262] == b"ustar" or lower.endswith(".tar"):
        return "tar"
    return None


def _preflight_zip(body: bytes, budget: ScanBudget) -> int:
    """先限定中央目录真实字节与记录数，再允许ZipFile创建对象。"""

    end = body.rfind(b"PK\x05\x06", max(0, len(body) - 65557))
    if end < 0 or end + _ZIP_END.size > len(body):
        raise ScanIncompleteError("scan_archive_invalid")
    _, disk, start_disk, disk_count, count, size, offset, comment = _ZIP_END.unpack_from(body, end)
    if disk or start_disk or disk_count != count or count == 65535 or size == 0xFFFFFFFF:
        raise ScanIncompleteError("scan_archive_unsupported")
    if count > budget.limits.archive_entries or size > budget.limits.central_directory_bytes:
        raise ScanIncompleteError("scan_archive_limit")
    if end + 22 + comment != len(body) or offset + size != end:
        raise ScanIncompleteError("scan_archive_invalid")
    cursor, actual = offset, 0
    records: list[tuple[int, ...]] = []
    while cursor < end:
        budget.checkpoint()
        if cursor + _ZIP_ENTRY.size > end:
            raise ScanIncompleteError("scan_archive_invalid")
        fields = _ZIP_ENTRY.unpack_from(body, cursor)
        if fields[0] != b"PK\x01\x02":
            raise ScanIncompleteError("scan_archive_invalid")
        actual += 1
        if actual > budget.limits.archive_entries:
            raise ScanIncompleteError("scan_archive_limit")
        # ZIP64、加密和多卷形式不在本扫描合同内；不交给隐式解密或未知算法。
        if fields[2] >= 45 or fields[3] & (1 | 64) or fields[4] not in _SUPPORTED_ZIP or fields[13]:
            raise ScanIncompleteError("scan_archive_unsupported")
        if fields[9] > budget.limits.member_bytes:
            raise ScanIncompleteError("scan_member_limit")
        records.append((fields[16], fields[3], fields[4], fields[7], fields[8], fields[9]))
        cursor += _ZIP_ENTRY.size + fields[10] + fields[11] + fields[12]
    if cursor != end or actual != count:
        raise ScanIncompleteError("scan_archive_invalid")
    _validate_zip_local_records(body, offset, records, budget)
    return count


def _validate_zip_local_records(
    body: bytes, directory_offset: int, records: list[tuple[int, ...]], budget: ScanBudget
) -> None:
    """每个本地记录必须被中央目录引用；不能隐藏额外压缩成员或前后缀载荷。"""

    cursor = 0
    for offset, flags, method, crc, compressed, size in sorted(records):
        budget.checkpoint()
        if offset != cursor or offset + _ZIP_LOCAL.size > directory_offset:
            raise ScanIncompleteError("scan_archive_invalid")
        local = _ZIP_LOCAL.unpack_from(body, offset)
        if local[0] != b"PK\x03\x04" or local[2:4] != (flags, method) or local[1] >= 45:
            raise ScanIncompleteError("scan_archive_invalid")
        cursor += _ZIP_LOCAL.size + local[9] + local[10] + compressed
        if flags & 8:
            if body[cursor : cursor + 4] == b"PK\x07\x08":
                cursor += 4
            if body[cursor : cursor + 12] != struct.pack("<III", crc, compressed, size):
                raise ScanIncompleteError("scan_archive_invalid")
            cursor += 12
        elif local[6:9] != (crc, compressed, size):
            raise ScanIncompleteError("scan_archive_invalid")
        if cursor > directory_offset:
            raise ScanIncompleteError("scan_archive_invalid")
    if cursor != directory_offset:
        raise ScanIncompleteError("scan_archive_invalid")


def _validate_member_name(name: str) -> None:
    """不解释宿主路径，拒绝发行物中的跨平台绝对路径和路径穿越声明。"""

    path = PurePosixPath(name)
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise ScanIncompleteError("scan_archive_entry_unsupported")


def _zip_members(body: bytes, budget: ScanBudget) -> Iterator[tuple[str, bytes]]:
    count = _preflight_zip(body, budget)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        entries = archive.infolist()
        if len(entries) != count:
            raise ScanIncompleteError("scan_archive_invalid")
        for entry in entries:
            budget.entry(member=True)
            _validate_member_name(entry.orig_filename)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ScanIncompleteError("scan_archive_entry_unsupported")
            if entry.is_dir():
                if entry.file_size:
                    raise ScanIncompleteError("scan_archive_invalid")
                continue
            data = _zip_member_data(body, entry, budget)
            budget.consume(len(data))
            yield entry.filename, data


def _zip_member_data(body: bytes, entry: zipfile.ZipInfo, budget: ScanBudget) -> bytes:
    """校验完整压缩区、解压长度与CRC，拒绝压缩流尾部隐藏数据。"""

    local = _ZIP_LOCAL.unpack_from(body, entry.header_offset)
    name_start = entry.header_offset + _ZIP_LOCAL.size
    encoded_name = body[name_start : name_start + local[9]]
    if encoded_name.decode("utf-8" if entry.flag_bits & 0x800 else "cp437") != entry.orig_filename:
        raise ScanIncompleteError("scan_archive_invalid")
    start = name_start + local[9] + local[10]
    compressed = body[start : start + entry.compress_size]
    if entry.compress_type == zipfile.ZIP_DEFLATED:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        data = decoder.decompress(compressed, budget.limits.member_bytes + 1)
        if len(data) > budget.limits.member_bytes:
            raise ScanIncompleteError("scan_member_limit")
        if not decoder.eof or decoder.unused_data:
            raise ScanIncompleteError("scan_archive_invalid")
    else:
        data = compressed
    if len(data) != entry.file_size or zlib.crc32(data) != entry.CRC:
        raise ScanIncompleteError("scan_archive_invalid")
    return data


def _decompress(body: bytes, kind: str, budget: ScanBudget) -> bytes:
    """先用max_length限制膨胀，再交给TAR解析，避免PAX长度驱动无界读取。"""

    cap = min(budget.limits.expanded_bytes, budget.limits.total_bytes - budget.bytes_read)
    if kind == "gz":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = decoder.decompress(body, cap + 1)
    elif kind == "bz2":
        decoder = bz2.BZ2Decompressor()
        data = decoder.decompress(body, max_length=cap + 1)
    else:
        decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
        data = decoder.decompress(body, max_length=cap + 1)
    if len(data) > cap:
        raise ScanIncompleteError("scan_byte_limit")
    if not decoder.eof or decoder.unused_data:
        # 多段压缩流不能只扫描第一段后报告通过。
        raise ScanIncompleteError("scan_archive_invalid")
    budget.consume(len(data))
    return data


def _preflight_tar(body: bytes, budget: ScanBudget, *, skip_links: bool = False) -> None:
    """有界检查实际TAR块；元数据记录同样计数，拒绝无终止块和隐藏尾部。"""

    offset, count = 0, 0
    while offset + 512 <= len(body):
        budget.checkpoint()
        header = body[offset : offset + 512]
        if not any(header):
            if len(body) - offset < 1024 or any(body[offset:]):
                raise ScanIncompleteError("scan_archive_invalid")
            return
        count += 1
        if count > budget.limits.archive_entries:
            raise ScanIncompleteError("scan_archive_limit")
        size_field = header[124:136].strip(b"\x00 ")
        if not size_field or any(byte not in b"01234567" for byte in size_field):
            raise ScanIncompleteError("scan_archive_unsupported")
        size = int(size_field, 8)
        entry_type = header[156:157]
        allowed = (b"0", b"\x00", b"5", b"x", b"g", b"L")
        if entry_type not in allowed and not (skip_links and entry_type in (b"1", b"2")):
            raise ScanIncompleteError("scan_archive_entry_unsupported")
        if entry_type in (b"5", b"1", b"2") and size:
            raise ScanIncompleteError("scan_archive_invalid")
        cap = 65536 if entry_type in (b"x", b"g", b"L") else budget.limits.member_bytes
        if size > cap:
            raise ScanIncompleteError("scan_member_limit")
        offset += 512 + ((size + 511) // 512) * 512
    raise ScanIncompleteError("scan_archive_invalid")


def _tar_members(
    body: bytes, budget: ScanBudget, *, skip_links: bool = False
) -> Iterator[tuple[str, bytes]]:
    # 许可证据采集可以跳过无载荷链接，但不解析目标；Secret扫描始终使用默认拒绝合同。
    _preflight_tar(body, budget, skip_links=skip_links)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r|") as archive:
        for entry in archive:
            budget.entry(member=True)
            _validate_member_name(entry.name)
            if entry.isdir():
                continue
            if skip_links and (entry.issym() or entry.islnk()):
                continue
            if not entry.isfile() or entry.sparse is not None:
                raise ScanIncompleteError("scan_archive_entry_unsupported")
            if entry.size > budget.limits.member_bytes:
                raise ScanIncompleteError("scan_member_limit")
            stream = archive.extractfile(entry)
            if stream is None:
                raise ScanIncompleteError("scan_archive_invalid")
            with stream:
                data = stream.read(budget.limits.member_bytes + 1)
            if len(data) != entry.size:
                raise ScanIncompleteError("scan_archive_invalid")
            budget.consume(len(data))
            yield entry.name, data


def archive_members(
    body: bytes, name: str, kind: str, budget: ScanBudget
) -> Iterator[tuple[str, bytes]]:
    """只返回成员字节，不解析或拼接宿主路径，也不调用extract/extractall。"""

    try:
        if kind == "zip":
            yield from _zip_members(body, budget)
        elif kind == "tar":
            yield from _tar_members(body, budget)
        else:
            expanded = _decompress(body, kind, budget)
            # 保留压缩流内的元数据和填充区扫描；下一层再按魔数识别TAR。
            lower = name.lower()
            decoded_name = name.rsplit(".", 1)[0]
            if lower.endswith((".tgz", ".tbz2", ".txz")):
                decoded_name += ".tar"
            budget.entry(member=True)
            yield decoded_name, expanded
    except ScanIncompleteError:
        raise
    except (
        OSError,
        ValueError,
        EOFError,
        RuntimeError,
        RecursionError,
        tarfile.TarError,
        zipfile.BadZipFile,
        lzma.LZMAError,
        zlib.error,
    ):
        raise ScanIncompleteError("scan_archive_invalid") from None
