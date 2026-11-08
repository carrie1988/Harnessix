"""GitHub原始文件离线镜像：按客户端实际发出的请求路径记录与查找。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from integrations.harbor.raw_mirror import HOST, LOCK_VERSION, load, lookup, wire_path

# matplotlib的dev-requirements以`-r ../testing/all.txt`引用其他清单，swebench拼出带`..`的URL；
# requests/urllib3发请求前会消去点段，镜像收到的是规整后的路径。
DOTTED = "matplotlib/matplotlib/f93c/requirements/dev/../testing/all.txt"
WIRE = "matplotlib/matplotlib/f93c/requirements/testing/all.txt"


def _mirror(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    cache = tmp_path / "mirror"
    for path, body in files.items():
        target = cache / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    lock = cache / "raw-mirror.lock.json"
    digests = {path: hashlib.sha256(body).hexdigest() for path, body in files.items()}
    lock.write_text(json.dumps({"spec_version": LOCK_VERSION, "host": HOST, "files": digests}))
    return cache, lock


def test_wire_path_matches_what_requests_sends() -> None:
    assert wire_path(DOTTED) == WIRE
    assert wire_path("django/django/abc/tests/requirements/py3.txt") == (
        "django/django/abc/tests/requirements/py3.txt"
    )


def test_lock_with_dot_segments_serves_the_normalized_request(tmp_path: Path) -> None:
    """锁中记录的是swebench拼出的原始URL：服务时按规整路径命中，查询串与编码不影响查找。"""
    cache, lock = _mirror(tmp_path, {DOTTED: b"pytest\n"})
    files = load(cache, lock)
    assert lookup(files, "/" + WIRE) == b"pytest\n"
    assert lookup(files, "/" + WIRE.replace("testing", "test%69ng") + "?token=x") == b"pytest\n"
    assert lookup(files, "/" + DOTTED) == b"pytest\n"
    assert lookup(files, "/matplotlib/matplotlib/f93c/requirements/other.txt") is None
