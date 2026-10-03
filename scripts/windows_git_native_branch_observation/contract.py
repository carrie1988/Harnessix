"""先核对离线合同，再下载唯一官方符号资产；没有任意资源入口。"""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

CONTRACT_SHA256 = "8e42b237a7c4fb666696b2cf2b68ad40d4de0dc003409fccfaa7c15a51a400b5"
CONTRACT_PATH = Path(__file__).with_name("contract.json")


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def read_contract(path: Path = CONTRACT_PATH) -> dict:
    body = path.read_bytes()
    # 只允许明确的checkout换行表示；不能容忍任意规范化后的内容漂移。
    if b"\r\n" in body:
        lf = body.replace(b"\r\n", b"\n")
        if body != lf.replace(b"\n", b"\r\n"):
            raise ValueError("offline_metadata_sha_mismatch")
    else:
        lf = body
    if hashlib.sha256(lf).hexdigest() != CONTRACT_SHA256:
        raise ValueError("offline_metadata_sha_mismatch")
    return json.loads(lf, object_pairs_hook=unique_object)


def source_checks(repository: Path, contract: dict) -> list[dict]:
    result = []
    for row in contract["source_inputs"]:
        body = (repository / row["path"]).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest == row["sha256"] and len(body) == row["bytes"]:
            representation = "EXACT_FROZEN_BYTES"
        elif digest == row["crlf_sha256"] and len(body) == row["crlf_bytes"]:
            representation = "EXACT_LF_TO_CRLF_TRANSFORM"
        else:
            raise ValueError("current_source_drift")
        result.append({"path": row["path"], "sha256": digest, "representation": representation})
    return result


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    """固定资源可使用官方资产CDN；不转发认证、Cookie或自定义凭据。"""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.hostname not in {
            "github.com",
            "release-assets.githubusercontent.com",
        }:
            raise ValueError("official_redirect_refused")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_symbols(output: Path, contract: dict) -> dict[str, Path]:
    asset = contract["assets"]["symbols"]
    opener = urllib.request.build_opener(OfficialRedirect())
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "Harnessix-native-v1"})
    started = time.monotonic()
    body = bytearray()
    with opener.open(request, timeout=10) as response:
        while chunk := response.read(65536):
            body.extend(chunk)
            if len(body) > asset["bytes"] or time.monotonic() - started > 30:
                raise ValueError("official_download_limit")
    if len(body) != asset["bytes"] or hashlib.sha256(body).hexdigest() != asset["sha256"]:
        raise ValueError("official_download_digest_mismatch")
    result = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for row in contract["pairs"]:
            members = [item for item in archive.infolist() if item.filename == row["pdb_member"]]
            if len(members) != 1 or members[0].file_size != row["pdb_bytes"]:
                raise ValueError("official_symbol_member_mismatch")
            content = archive.read(members[0])
            if hashlib.sha256(content).hexdigest() != row["pdb_sha256"]:
                raise ValueError("official_symbol_member_mismatch")
            directory = output / "symbols" / row["role"]
            directory.mkdir(parents=True)
            path = directory / "git.pdb"
            path.write_bytes(content)
            result[row["role"]] = path
    return result
