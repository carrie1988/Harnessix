"""许可证结论必须来自锁定发行物，而不是执行门禁的本机安装环境。"""

from pathlib import Path

import pytest

from scripts.license_scan import build_report


@pytest.mark.parametrize("version", ["0.0.0", "999.0.0"])
def test_source_less_fake_installed_version_cannot_be_approved(
    tmp_path: Path, version: str
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(f'[[package]]\nname="pytest"\nversion="{version}"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        build_report(lock, {"allow": ["MIT License"], "deny": []})


def test_name_only_override_cannot_approve_archive_without_evidence(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname="evil-lib"\nversion="1.0"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        build_report(
            lock, {"allow": ["MIT"], "overrides": [{"name": "evil-lib", "declared_license": "MIT"}]}
        )


def _policy() -> dict:
    return {
        "spec_version": "harnessix.license-policy/v2",
        "allow": ["MIT", "Apache-2.0"],
        "deny": ["GPL-3.0-only", "LGPL-2.1-only"],
        "allow_exceptions": [],
        "reviews": [],
    }


def _wheel(
    metadata: bytes, notice: bytes = b"Public MIT notice", extra: dict | None = None
) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sample-1.0.dist-info/METADATA", metadata)
        archive.writestr("sample-1.0.dist-info/licenses/LICENSE", notice)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _metadata(expression: str = "MIT", extra: str = "", version: str = "1.0") -> bytes:
    return (
        f"Metadata-Version: 2.4\nName: sample\nVersion: {version}\n"
        f"License-Expression: {expression}\nLicense-File: LICENSE\n{extra}\nPublic description\n"
    ).encode()


def _fixture(root: Path, body: bytes | None = None) -> tuple[Path, Path, dict]:
    from scripts.license_contracts import digest, locked_inventory
    from scripts.license_inventory import collect_inventory

    root = root.resolve()
    project = root / "pyproject.toml"
    project.write_text(
        '[project]\nname="harnessix"\nversion="0.1.0"\nlicense="AGPL-3.0-only"\nlicense-files=["LICENSE"]\n'
    )
    (root / "LICENSE").write_text("Public root license fixture\n")
    body = body or _wheel(_metadata())
    sha = digest(body)
    lock = root / "uv.lock"
    lock.write_text(
        'version=1\n[[package]]\nname="harnessix"\nversion="0.1.0"\nsource={editable="."}\n'
        '[[package]]\nname="sample"\nversion="1.0"\nsource={registry="https://pypi.org/simple"}\n'
        'wheels=[{url="https://files.pythonhosted.org/packages/sample.whl", '
        f'hash="sha256:{sha}", size={len(body)}}}]\n'
    )
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    (cache / sha).write_bytes(body)
    evidence = root / "governance/license-evidence-v2"
    collect_inventory(lock, project, evidence, cache, fetch=False)
    return lock, evidence, locked_inventory(lock, project)[1][0].identity()


def test_archive_report_is_offline_complete_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.metadata

    from scripts import license_inventory
    from scripts.license_contracts import canonical_bytes

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("离线报告不得读取本机发行版或访问网络")

    monkeypatch.setattr(importlib.metadata, "metadata", forbidden)
    monkeypatch.setattr(license_inventory, "download_archive", forbidden)
    lock, _, _ = _fixture(tmp_path)
    report = build_report(lock, _policy())
    assert canonical_bytes(report) == canonical_bytes(build_report(lock, _policy()))
    assert report["package_count"] == 2 and report["archive_count"] == 1
    assert report["violation_count"] == 0
    assert report["entries"][0]["version"] == "1.0"
    assert report["archive_reextraction_performed"] is False
    assert report["legal_rights_or_notice_completeness_claimed"] is False


@pytest.mark.parametrize(
    "expression,reason",
    [
        ("MIT OR GPL-3.0-only", "license_expression_denied"),
        ("MIT AND Apache-2.0", "license_expression_allowed"),
        ("MIT OR Apache-2.0", "license_expression_allowed"),
        ("LicenseRef-Unreviewed", "license_expression_not_allowed"),
        ("Proprietary", "license_expression_unknown"),
        ("MIT OR", "license_expression_unknown"),
        ("Apache-2.0 WITH LLVM-exception", "license_exception_not_allowed"),
    ],
)
def test_spdx_grammar_and_conservative_policy(expression: str, reason: str) -> None:
    from scripts.license_decisions import decide, validate_policy

    policy = _policy()
    validate_policy(policy)
    status, actual, _ = decide(expression, policy)
    assert actual == reason
    assert status == ("allow" if reason == "license_expression_allowed" else "violation")


def test_with_exception_requires_explicit_pair() -> None:
    from scripts.license_decisions import decide, validate_policy

    policy = _policy()
    policy["allow_exceptions"] = ["Apache-2.0 WITH LLVM-exception"]
    validate_policy(policy)
    assert decide("(MIT AND Apache-2.0 WITH LLVM-exception)", policy)[0] == "allow"


@pytest.mark.parametrize(
    "mutation", ["version", "source", "root", "url", "size", "missing-archive", "duplicate"]
)
def test_lock_identity_drift_cannot_inherit_old_evidence(tmp_path: Path, mutation: str) -> None:
    from scripts.license_contracts import LicenseEvidenceError

    lock, _, _ = _fixture(tmp_path)
    text = lock.read_text()
    if mutation == "version":
        text = text.replace('version="1.0"', 'version="999.0.0"')
    elif mutation == "source":
        text = text.replace("https://pypi.org/simple", "https://other.example/simple")
    elif mutation == "root":
        text = text.replace('source={editable="."}', 'source={registry="https://pypi.org/simple"}')
    elif mutation == "url":
        text = text.replace("files.pythonhosted.org", "evil.example")
    elif mutation == "size":
        text = text.replace("size=", "size=33554432#")
    elif mutation == "missing-archive":
        text = text[: text.index("wheels=")] + "wheels=[]\n"
    else:
        text += text[text.index('[[package]]\nname="sample"') :]
    lock.write_text(text)
    with pytest.raises(LicenseEvidenceError):
        build_report(lock, _policy())


@pytest.mark.parametrize(
    "mutation", ["hash", "missing", "count", "identity", "duplicate", "notice", "blob"]
)
def test_inventory_or_blob_tamper_blocks(tmp_path: Path, mutation: str) -> None:
    from scripts.license_contracts import LicenseEvidenceError, canonical_bytes, read_json

    lock, evidence, _ = _fixture(tmp_path)
    index = read_json(evidence / "index.json")
    entry = index["entries"][0]
    if mutation == "hash":
        index["lock_sha256"] = "0" * 64
    elif mutation == "missing":
        index["entries"] = []
    elif mutation == "count":
        index["archive_count"] = 999
    elif mutation == "identity":
        entry["version"] = "2.0"
    elif mutation == "duplicate":
        index["entries"].append(entry)
    elif mutation == "notice":
        entry["notices"] = []
    else:
        (evidence / "blobs" / entry["metadata"]["sha256"]).write_bytes(b"tampered")
    (evidence / "index.json").write_bytes(canonical_bytes(index))
    with pytest.raises(LicenseEvidenceError):
        build_report(lock, _policy())


@pytest.mark.parametrize(
    "extra,version,code",
    [
        ("Name: other\n", "1.0", "license_metadata_duplicate_field"),
        ("License: MIT\n", "1.0", "license_metadata_conflicting_fields"),
        ("", "2.0", "license_metadata_identity_mismatch"),
        ("License-File: ../private\n", "1.0", "license_notice_path_invalid"),
        ("License-File: LICENSE\n", "1.0", "license_metadata_duplicate_field"),
    ],
)
def test_metadata_identity_and_conflict_fail_closed(
    tmp_path: Path, extra: str, version: str, code: str
) -> None:
    from scripts.license_contracts import LicenseEvidenceError

    # 采集与离线核对均可阻断；没有通过采集即代表输入不可形成完整证据。
    with pytest.raises(LicenseEvidenceError, match=code):
        lock, _, _ = _fixture(tmp_path, _wheel(_metadata(extra=extra, version=version)))
        build_report(lock, _policy())


def test_declared_arbitrary_notice_filename_is_preserved(tmp_path: Path) -> None:
    body = _wheel(
        _metadata(extra="License-File: CREDITS.rst\n"),
        extra={
            "sample-1.0.dist-info/licenses/CREDITS.rst": b"Public credits",
        },
    )
    lock, _, _ = _fixture(tmp_path, body)
    report = build_report(lock, _policy())
    assert len(report["entries"][0]["notices"]) == 2
    assert report["violation_count"] == 0


def test_restricted_notice_cannot_be_hidden_by_permissive_metadata(tmp_path: Path) -> None:
    lock, _, _ = _fixture(
        tmp_path, _wheel(_metadata(), notice=b"GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\n")
    )
    report = build_report(lock, _policy())
    assert report["violation_count"] == 1
    assert report["entries"][0]["reason"] == "license_notice_review_required"
    assert report["entries"][0]["notice_review_signals"]


def test_review_is_bound_to_exact_archive_not_name(tmp_path: Path) -> None:
    from scripts.license_contracts import LicenseEvidenceError, read_json

    body = _wheel(_metadata().replace(b"License-Expression: MIT", b"License: BSD"))
    lock, evidence, identity = _fixture(tmp_path, body)
    record = read_json(evidence / "index.json")["entries"][0]
    policy = _policy()
    policy["reviews"] = [
        {
            "review_id": "sample-1.0-wheel",
            "expression": "MIT",
            "basis": "公开测试收据，不构成实际许可判断。",
            "binding": {
                **identity,
                "metadata_sha256": record["metadata"]["sha256"],
                "notice_sha256": sorted(n["sha256"] for n in record["notices"]),
            },
        }
    ]
    assert build_report(lock, policy)["violation_count"] == 0
    policy["reviews"][0]["binding"]["version"] = "0.9"
    with pytest.raises(LicenseEvidenceError, match="license_review_stale"):
        build_report(lock, policy)


def test_explicit_spdx_cannot_be_replaced_by_review(tmp_path: Path) -> None:
    from scripts.license_contracts import LicenseEvidenceError, read_json

    lock, evidence, identity = _fixture(tmp_path)
    record = read_json(evidence / "index.json")["entries"][0]
    policy = _policy()
    policy["reviews"] = [
        {
            "review_id": "bad-review",
            "expression": "Apache-2.0",
            "basis": "测试非法覆盖",
            "binding": {
                **identity,
                "metadata_sha256": record["metadata"]["sha256"],
                "notice_sha256": sorted(n["sha256"] for n in record["notices"]),
            },
        }
    ]
    with pytest.raises(LicenseEvidenceError, match="license_review_invalid"):
        build_report(lock, policy)


def test_json_duplicate_keys_block(tmp_path: Path) -> None:
    from scripts.license_contracts import LicenseEvidenceError, read_json

    path = tmp_path / "policy.json"
    path.write_text('{"allow":[],"allow":["MIT"]}')
    with pytest.raises(LicenseEvidenceError, match="license_json_duplicate_key"):
        read_json(path)


def test_missing_cache_does_not_fetch_or_replace_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import license_inventory
    from scripts.license_contracts import LicenseEvidenceError

    lock, evidence, identity = _fixture(tmp_path)
    old = (evidence / "index.json").read_bytes()
    (tmp_path / "cache" / identity["sha256"]).unlink()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("默认不得联网")

    monkeypatch.setattr(license_inventory, "download_archive", forbidden)
    with pytest.raises(LicenseEvidenceError, match="license_cache_missing"):
        license_inventory.collect_inventory(
            lock, lock.parent / "pyproject.toml", evidence, tmp_path / "cache", fetch=False
        )
    assert (evidence / "index.json").read_bytes() == old


def test_cancelled_collection_and_scan_have_fixed_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from scripts import license_inventory, license_scan

    def cancelled(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(license_inventory, "collect_inventory", cancelled)
    assert license_inventory.main([]) == 2
    monkeypatch.setattr(license_scan, "build_report", cancelled)
    assert license_scan.main([]) == 2
    output = capsys.readouterr().err
    assert output.count("license_cancelled") == 2 and "Traceback" not in output


def test_root_cli_check_is_readonly_and_returns_policy_failure(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    committed = json.loads((root / "governance/license-scan-v2.json").read_bytes())
    output = tmp_path / "report.json"
    output.write_bytes(b"{}\n")
    run = subprocess.run(
        [sys.executable, "-m", "scripts.license_scan", "--check", "--output", str(output)],
        cwd=root,
        capture_output=True,
        timeout=30,
    )
    assert run.returncode == 1 and output.read_bytes() == b"{}\n"
    assert committed["archive_count"] == 777 and committed["violation_count"] == 12
    assert {e["name"] for e in committed["entries"] if e["status"] != "allow"} == {"pywin32"}


@pytest.mark.parametrize(
    "mutation", ["tail", "hash", "truncated", "redirect", "network", "timeout"]
)
def test_download_mismatch_or_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import io
    import urllib.error

    from scripts import license_inventory
    from scripts.license_contracts import LicenseEvidenceError, LockedArchive

    lock, _, identity = _fixture(tmp_path)
    expected = (lock.parent / "cache" / identity["sha256"]).read_bytes()
    body = expected + b"tail" if mutation == "tail" else expected
    if mutation == "hash":
        body = b"x" * len(expected)
    if mutation == "truncated":
        body = expected[:-1]
    calls = []

    class Response(io.BytesIO):
        status = 200

        def geturl(self) -> str:
            return "https://evil.example/private" if mutation == "redirect" else identity["url"]

    class Opener:
        def open(self, url: str, timeout: float) -> Response:
            calls.append((url, timeout))
            if mutation == "network":
                raise urllib.error.URLError("private connection details")
            return Response(body)

    handlers = []

    def opener(*args: object) -> Opener:
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(license_inventory.urllib.request, "build_opener", opener)
    if mutation == "timeout":
        ticks = iter([0.0, 61.0])
        monkeypatch.setattr(license_inventory.time, "monotonic", lambda: next(ticks))
    with pytest.raises(LicenseEvidenceError) as caught:
        license_inventory.download_archive(LockedArchive(**identity))
    assert "private" not in str(caught.value)
    assert len(calls) == 1
    proxy = next(
        h for h in handlers if isinstance(h, license_inventory.urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(isinstance(h, license_inventory._NoRedirect) for h in handlers)


def test_download_checks_exact_locked_size_hash_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from scripts import license_inventory
    from scripts.license_contracts import LockedArchive

    lock, _, identity = _fixture(tmp_path)
    body = (lock.parent / "cache" / identity["sha256"]).read_bytes()

    class Response(io.BytesIO):
        status = 200

        def geturl(self) -> str:
            return identity["url"]

    class Opener:
        def open(self, url: str, timeout: float) -> Response:
            assert url == identity["url"] and timeout == 20
            return Response(body)

    monkeypatch.setattr(license_inventory.urllib.request, "build_opener", lambda *args: Opener())
    assert license_inventory.download_archive(LockedArchive(**identity)) == body


def test_tar_ignores_unrelated_links_but_never_follows_license_links(tmp_path: Path) -> None:
    import gzip
    import io
    import tarfile

    from scripts.license_contracts import LicenseEvidenceError, LockedArchive, digest
    from scripts.license_inventory import archive_evidence

    for linked_license in (False, True):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            files = {"sample-1.0/PKG-INFO": _metadata()}
            if not linked_license:
                files["sample-1.0/LICENSE"] = b"Public notice"
            for name, content in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            entry = tarfile.TarInfo(
                "sample-1.0/LICENSE" if linked_license else "sample-1.0/CHANGELOG"
            )
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../../private"
            archive.addfile(entry)
        body = gzip.compress(buffer.getvalue())
        locked = LockedArchive(
            "sample",
            "1.0",
            "https://pypi.org/simple",
            "sdist",
            "https://files.pythonhosted.org/packages/sample.tar.gz",
            digest(body),
            len(body),
        )
        if linked_license:
            with pytest.raises(LicenseEvidenceError, match="license_declared_notice_missing"):
                archive_evidence(body, locked)
        else:
            record, blobs = archive_evidence(body, locked)
            assert len(record["notices"]) == 1 and len(blobs) == 2


def test_evidence_budget_and_cached_size_are_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import license_contracts
    from scripts.license_contracts import EvidenceBlobs, LicenseEvidenceError, blob_record

    root = tmp_path.resolve()
    directory = root / "blobs"
    directory.mkdir()
    body = b"public"
    record = blob_record(body, "LICENSE")
    (directory / record["sha256"]).write_bytes(body)
    store = EvidenceBlobs(root)
    assert store.read(record) == store.read(record) == body and store.bytes_read == len(body)
    with pytest.raises(LicenseEvidenceError, match="license_blob_mismatch"):
        store.read({**record, "size_bytes": 100})
    store = EvidenceBlobs(root)
    store.bytes_read = 64 * 1024**2
    with pytest.raises(LicenseEvidenceError, match="license_evidence_limit"):
        store.read(record)
    store = EvidenceBlobs(root)
    monkeypatch.setattr(license_contracts.time, "monotonic", lambda: store.started + 61)
    with pytest.raises(LicenseEvidenceError, match="license_verification_timeout"):
        store.read(record)


def test_multiline_unknown_license_cannot_fall_back_to_mit_classifier(tmp_path: Path) -> None:
    metadata = _metadata().replace(
        b"License-Expression: MIT",
        b"License: Proprietary declaration\n Continuation not an SPDX expression\n"
        b"Classifier: License :: OSI Approved :: MIT License",
    )
    lock, _, _ = _fixture(tmp_path, _wheel(metadata))
    report = build_report(lock, _policy())
    assert report["violation_count"] == 1
    assert report["entries"][0]["declaration_source"] == "review_required"


@pytest.mark.parametrize(
    "header", [b"Metadata-Version: 2.7", b"Metadata-Version: 3.0", b"Metadata-Version: broken"]
)
def test_unknown_metadata_version_blocks_before_inventory_publication(
    tmp_path: Path, header: bytes
) -> None:
    from scripts.license_contracts import LicenseEvidenceError

    body = _wheel(_metadata().replace(b"Metadata-Version: 2.4", header))
    with pytest.raises(LicenseEvidenceError, match="license_metadata_invalid"):
        _fixture(tmp_path, body)


def test_invalid_policy_keys_and_spdx_are_not_permitted() -> None:
    from scripts.license_contracts import LicenseEvidenceError
    from scripts.license_decisions import validate_policy

    for policy in (
        {**_policy(), "overrides": []},
        {**_policy(), "allow": ["Proprietary"]},
        {**_policy(), "reviews": [{"review_id": "name-only"}]},
    ):
        with pytest.raises(LicenseEvidenceError):
            validate_policy(policy)


def test_input_group_change_during_report_is_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import license_scan
    from scripts.license_contracts import LicenseEvidenceError

    lock, _, _ = _fixture(tmp_path)
    original = license_scan.locked_inventory

    def changed(lock_path: Path, project_path: Path) -> tuple:
        result = original(lock_path, project_path)
        project_path.write_bytes(project_path.read_bytes() + b"\n# concurrent edit\n")
        return result

    monkeypatch.setattr(license_scan, "locked_inventory", changed)
    with pytest.raises(LicenseEvidenceError, match="license_inputs_changed"):
        build_report(lock, _policy())
