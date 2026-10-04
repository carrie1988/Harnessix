"""已评审角色接线的精确发行输入；不执行原生观察或下载。"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.windows_git_native_branch_observation import contract

ROOT = Path(__file__).resolve().parents[2]
BASE = "5265fdf2d1755b15491f41b770b2d718bab98d2c"
METADATA = "scripts/windows_git_native_branch_observation/contract.json"
PARSER = "scripts/windows_git_native_branch_observation/contract.py"
CHANGED_INPUTS = {
    "src/harnessix/product_config/git_delivery_process.py",
    "src/harnessix/product_config/git_material_process.py",
    "tests/product_config/git_minimum_commit_probe.py",
    "tests/product_config/git_trace2_projection.py",
}
PROBE = "tests/product_config/git_minimum_commit_probe.py"
PROJECTION = "tests/product_config/git_trace2_projection.py"
ROLE_REVISION = "9a0d84aaba6243539abd63e2de69b482350486a2"
DIAGNOSTIC_BASE = "5306c7134c1301dd10bee682be5ce1e61e120c46"
DIAGNOSTIC_REVISION = "9b9e52fdaab74567b5ad7cd5614801f1936689bc"
LIVE_CATALOG_REVISION = "0583b53306f3ab869fb35c5b9eece80fbe2251a4"
DIAGNOSTIC_INPUTS = (
    "src/harnessix/delivery/git_material_trace2_profile.py",
    "scripts/windows_git_native_branch_observation/failure_projection.py",
)
PROTECTED = {
    PROBE: (
        "_post_once",
        "_success",
        "_install",
        "_async_wrapper",
        "_sync_wrapper",
        "_operation_wrapper",
        "_errors",
        "_receipt",
        "_initialize",
        "Probe.render",
    ),
    PROJECTION: (
        "_Stream",
        "project_git_trace2_events",
        "_consume_frame",
        "_decode_frame",
        "_valid_event",
        "_record",
        "initialize_operation_trace2",
        "diagnostic_probes_complete",
    ),
}


def _original(path: str, *, revision: str = BASE) -> bytes:
    return subprocess.run(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT, check=True, capture_output=True
    ).stdout


def _segment(body: bytes, qualified_name: str) -> str:
    text = body.decode("utf-8")
    nodes = ast.parse(text).body
    for name in qualified_name.split("."):
        selected = next(node for node in nodes if getattr(node, "name", None) == name)
        nodes = getattr(selected, "body", [])
    result = ast.get_source_segment(text, selected)
    assert result is not None
    return result


def _metadata_changes(left, right, path="") -> set[str]:
    """类型敏感地定位合同差分，避免Python把整数、浮点数和布尔值视为相等。"""
    if type(left) is not type(right):
        return {path}
    if type(left) is dict:
        if left.keys() != right.keys():
            return {path}
        return set().union(
            *(_metadata_changes(value, right[key], f"{path}/{key}") for key, value in left.items())
        )
    if type(left) is list:
        if len(left) != len(right):
            return {path}
        return set().union(
            *(
                _metadata_changes(a, b, f"{path}/{index}")
                for index, (a, b) in enumerate(zip(left, right, strict=True))
            )
        )
    return set() if left == right else {path}


def test_current_complete_source_identity_is_accepted():
    rows = contract.source_checks(ROOT, contract.read_contract())
    assert len(rows) == 18
    assert all(row["representation"] == "EXACT_FROZEN_BYTES" for row in rows)


def test_new_metadata_changes_only_four_source_rows_and_published_baseline():
    original = json.loads(_original(METADATA))
    # 角色合同保留固定历史17叶差分；后继诊断目录不能覆盖或改写该已评审事实。
    current = json.loads(
        subprocess.run(
            ["git", "show", f"{ROLE_REVISION}:{METADATA}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    )
    restored = copy.deepcopy(current)
    assert current["base_revision"] == BASE
    restored["base_revision"] = original["base_revision"]
    changed = set()
    allowed = {"/base_revision"}
    for index, (old, new) in enumerate(
        zip(original["source_inputs"], restored["source_inputs"], strict=True)
    ):
        assert old["path"] == new["path"]
        if _metadata_changes(old, new):
            changed.add(new["path"])
            assert set(old) == set(new) == {"path", "bytes", "sha256", "crlf_bytes", "crlf_sha256"}
            body = (ROOT / new["path"]).read_bytes()
            crlf = body.replace(b"\n", b"\r\n")
            expected = {
                "path": old["path"],
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "crlf_bytes": len(crlf),
                "crlf_sha256": hashlib.sha256(crlf).hexdigest(),
            }
            assert not _metadata_changes(expected, new)
            allowed.update(
                f"/source_inputs/{index}/{field}"
                for field in ("bytes", "sha256", "crlf_bytes", "crlf_sha256")
            )
            new.update(old)
    assert changed == CHANGED_INPUTS
    assert not _metadata_changes(original, restored)
    assert len(allowed) == 17
    assert _metadata_changes(original, current) == allowed


def test_new_metadata_appends_only_two_exact_diagnostic_members():
    original = json.loads(
        subprocess.run(
            ["git", "show", f"{DIAGNOSTIC_BASE}:{METADATA}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    )
    live = contract.read_contract()
    _assert_live_source_catalog_identity_delta(live)
    assert len(contract.source_checks(ROOT, live)) == 18
    current = json.loads(_original(METADATA, revision=DIAGNOSTIC_REVISION))
    assert current["base_revision"] == DIAGNOSTIC_BASE
    assert len(current["source_inputs"]) == 18
    assert not _metadata_changes(current["source_inputs"][:16], original["source_inputs"])
    for row, name in zip(current["source_inputs"][16:], DIAGNOSTIC_INPUTS, strict=True):
        body = _original(name, revision=DIAGNOSTIC_REVISION)
        crlf = body.replace(b"\n", b"\r\n")
        expected = dict(
            path=name,
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            crlf_bytes=len(crlf),
            crlf_sha256=hashlib.sha256(crlf).hexdigest(),
        )
        assert not _metadata_changes(expected, row)
    restored = copy.deepcopy(current)
    restored["base_revision"] = original["base_revision"]
    restored["source_inputs"] = restored["source_inputs"][:16]
    assert not _metadata_changes(original, restored)
    assert _metadata_changes(original, current) == {"/base_revision", "/source_inputs"}


def _assert_live_source_catalog_identity_delta(current):
    """类型敏感地核对后继四叶；其他身份、预算与原行不能被等值类型绕过。"""
    original = json.loads(_original(METADATA, revision=LIVE_CATALOG_REVISION))
    index = next(
        i
        for i, row in enumerate(original["source_inputs"])
        if row["path"] == "src/harnessix/delivery/git.py"
    )
    assert _metadata_changes(original, current) == {
        f"/source_inputs/{index}/{name}"
        for name in ("bytes", "sha256", "crlf_bytes", "crlf_sha256")
    }
    assert len(current["source_inputs"]) == 18
    row = current["source_inputs"][index]
    body = (ROOT / row["path"]).read_bytes()
    crlf = body.replace(b"\n", b"\r\n")
    expected = {
        "path": row["path"],
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "crlf_bytes": len(crlf),
        "crlf_sha256": hashlib.sha256(crlf).hexdigest(),
    }
    assert not _metadata_changes(expected, row)


def test_live_source_catalog_changes_only_reviewed_git_identity_leaves():
    """后继来源端口只重绑既有Git输入，不改历史追加事实或原18项准入。"""
    current = contract.read_contract()
    _assert_live_source_catalog_identity_delta(current)
    assert len(contract.source_checks(ROOT, current)) == 18


def test_live_source_catalog_refuses_previous_git_bytes(tmp_path):
    current = contract.read_contract()
    row = next(r for r in current["source_inputs"] if r["path"] == "src/harnessix/delivery/git.py")
    destination = tmp_path / row["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_original(row["path"], revision=LIVE_CATALOG_REVISION))
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(tmp_path, {"source_inputs": [row]})


def test_parser_changes_only_contract_digest_literal():
    original = _original(PARSER)
    old_digest = hashlib.sha256(_original(METADATA)).hexdigest().encode()
    new_digest = hashlib.sha256((ROOT / METADATA).read_bytes()).hexdigest().encode()
    assert old_digest != new_digest
    assert original.count(old_digest) == 1
    assert (ROOT / PARSER).read_bytes() == original.replace(old_digest, new_digest)


def test_old_metadata_continues_to_refuse_current_candidate():
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(ROOT, json.loads(_original(METADATA)))


@pytest.mark.parametrize("path", sorted(CHANGED_INPUTS))
def test_old_identity_refuses_each_known_changed_member(path):
    old = json.loads(_original(METADATA))
    row = next(row for row in old["source_inputs"] if row["path"] == path)
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(ROOT, {"source_inputs": [row]})


@pytest.mark.parametrize("index", range(18))
def test_every_registered_source_refuses_new_unreviewed_byte(index, tmp_path):
    row = contract.read_contract()["source_inputs"][index]
    destination = tmp_path / row["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((ROOT / row["path"]).read_bytes() + b" ")
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(tmp_path, {"source_inputs": [row]})


@pytest.mark.parametrize("index", range(18))
def test_every_registered_source_accepts_only_frozen_crlf(index, tmp_path):
    row = contract.read_contract()["source_inputs"][index]
    destination = tmp_path / row["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((ROOT / row["path"]).read_bytes().replace(b"\n", b"\r\n"))
    assert contract.source_checks(tmp_path, {"source_inputs": [row]}) == [
        {
            "path": row["path"],
            "sha256": row["crlf_sha256"],
            "representation": "EXACT_LF_TO_CRLF_TRANSFORM",
        }
    ]


@pytest.mark.parametrize("representation", ["crlf", "mixed", "space", "semantic_json"])
def test_metadata_representation_is_exact_not_semantic(representation, tmp_path):
    body = (ROOT / METADATA).read_bytes()
    if representation == "crlf":
        body = body.replace(b"\n", b"\r\n")
    elif representation == "mixed":
        body = body.replace(b"\n", b"\r\n", 1)
    elif representation == "space":
        body += b" "
    else:
        body = json.dumps(json.loads(body), sort_keys=True).encode()
    path = tmp_path / "contract.json"
    path.write_bytes(body)
    if representation == "crlf":
        assert contract.read_contract(path) == contract.read_contract()
    else:
        with pytest.raises(ValueError, match="^offline_metadata_sha_mismatch$"):
            contract.read_contract(path)


@pytest.mark.parametrize("field", ["bytes", "sha256", "crlf_bytes", "crlf_sha256"])
def test_source_length_and_digest_must_match_same_representation(field, tmp_path):
    row = copy.deepcopy(contract.read_contract()["source_inputs"][0])
    destination = tmp_path / row["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = (ROOT / row["path"]).read_bytes()
    destination.write_bytes(body.replace(b"\n", b"\r\n") if field.startswith("crlf") else body)
    row[field] = row[field] + 1 if isinstance(row[field], int) else "0" * 64
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(tmp_path, {"source_inputs": [row]})


@pytest.mark.parametrize(
    "path,symbol", [(path, symbol) for path, symbols in PROTECTED.items() for symbol in symbols]
)
def test_original_raw_hooks_stream_and_publication_are_exact(path, symbol):
    assert _segment((ROOT / path).read_bytes(), symbol) == _segment(_original(path), symbol)


@pytest.mark.parametrize(
    "location,kind",
    [
        (("pairs", 0, "age"), "bool"),
        (("pairs", 0, "age"), "float"),
        (("pairs", 1, "age"), "bool"),
        (("budgets", "command_seconds"), "float"),
        (("assets", "reference_binary", "bytes"), "float"),
        (("source_inputs", 0, "bytes"), "float"),
        (("source_inputs", 5, "bytes"), "float"),
        (("source_inputs", 5, "crlf_bytes"), "float"),
        (("source_inputs", 6, "bytes"), "float"),
        (("source_inputs", 10, "bytes"), "float"),
        (("source_inputs", 16, "bytes"), "float"),
        (("source_inputs", 16, "crlf_bytes"), "float"),
        (("source_inputs", 17, "bytes"), "float"),
        (("source_inputs", 17, "crlf_bytes"), "float"),
    ],
)
def test_metadata_delta_rejects_python_equal_but_different_json_types(location, kind, monkeypatch):
    current = copy.deepcopy(contract.read_contract())
    target = current
    for key in location[:-1]:
        target = target[key]
    original = target[location[-1]]
    assert type(original) is int
    target[location[-1]] = bool(original) if kind == "bool" else float(original)
    assert target[location[-1]] == original
    monkeypatch.setattr(contract, "read_contract", lambda: current)
    with pytest.raises(AssertionError):
        test_new_metadata_appends_only_two_exact_diagnostic_members()
