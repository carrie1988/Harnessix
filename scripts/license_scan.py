"""离线核对全部锁定Archive的版本/来源/许可原字节证据，报告漂移失败关闭。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cli_console import configure_utf8_console
from scripts.license_contracts import (
    EvidenceBlobs,
    LicenseEvidenceError,
    LockedArchive,
    canonical_bytes,
    digest,
    locked_inventory,
    read_input,
    read_json,
)
from scripts.license_decisions import (
    decide,
    metadata_facts,
    notice_signals,
    reviewed_expression,
    validate_policy,
)
from scripts.license_inventory import EVIDENCE_DIRECTORY, write_atomic

POLICY_FILENAME = "governance/license-policy-v2.json"
REPORT_FILENAME = "governance/license-scan-v2.json"


def _archive_decision(
    archive: LockedArchive, record: dict | None, policy: dict, blobs: EvidenceBlobs
) -> dict:
    """只负责一份Archive的证据验证与许可决定；不维护批次索引或发布资格。"""

    if record is None or any(record.get(key) != value for key, value in archive.identity().items()):
        raise LicenseEvidenceError("license_inventory_identity_mismatch")
    metadata = blobs.read(record["metadata"])
    notices = record["notices"]
    if not isinstance(notices, list) or not 1 <= len(notices) <= 128:
        raise LicenseEvidenceError("license_notice_missing")
    names = [item["member"] for item in notices]
    if len(names) != len(set(names)):
        raise LicenseEvidenceError("license_notice_duplicate")
    signals = []
    for notice in notices:
        for signal in notice_signals(blobs.read(notice), policy):
            signals.append({"sha256": notice["sha256"], "reason": signal})
    facts = metadata_facts(metadata, archive, record["metadata"]["member"], notices)
    expression, source, review_id = reviewed_expression(record, policy, facts)
    status, reason, canonical = decide(expression, policy)
    if signals:
        status, reason = "violation", "license_notice_review_required"
    return {
        **archive.identity(),
        **facts,
        "decision_source": source,
        "canonical_expression": canonical,
        "status": status,
        "reason": reason,
        "review_id": review_id,
        "metadata": record["metadata"],
        "notices": notices,
        "notice_review_signals": signals,
    }


def _build_report(
    lock_path: Path,
    policy: dict,
    *,
    project_path: Path | None = None,
    evidence_directory: Path | None = None,
) -> dict:
    """锁集合、索引与Blob必须全覆盖；不查本机安装、不隐式联网、不自动修复。"""

    project_path = project_path or lock_path.parent / "pyproject.toml"
    evidence_directory = evidence_directory or lock_path.parent / EVIDENCE_DIRECTORY
    index_path = evidence_directory / "index.json"
    license_path = project_path.parent / "LICENSE"
    frozen = {
        path: read_input(path) for path in (lock_path, project_path, index_path, license_path)
    }
    project, archives = locked_inventory(lock_path, project_path)
    validate_policy(policy)
    index = read_json(index_path)
    if (
        index.get("spec_version") != "harnessix.license-evidence/v2"
        or index.get("collector_version") != "harnessix.license-inventory/v2"
        or index.get("lock_sha256") != digest(frozen[lock_path])
        or index.get("archive_count") != len(archives)
        or not isinstance(index.get("entries"), list)
        or len(index["entries"]) != len(archives)
    ):
        raise LicenseEvidenceError("license_inventory_mismatch")
    records = {record["sha256"]: record for record in index["entries"]}
    if len(records) != len(archives):
        raise LicenseEvidenceError("license_inventory_duplicate")
    blobs = EvidenceBlobs(evidence_directory)
    entries = [
        _archive_decision(archive, records.get(archive.sha256), policy, blobs)
        for archive in archives
    ]
    used_reviews = {entry["review_id"] for entry in entries if entry["review_id"]}
    reviews = policy.get("reviews", [])
    if len(used_reviews) != len(reviews) or len({item["review_id"] for item in reviews}) != len(
        reviews
    ):
        raise LicenseEvidenceError("license_review_stale")
    if project.get("license") != "AGPL-3.0-only" or project.get("license-files") != ["LICENSE"]:
        raise LicenseEvidenceError("license_root_license_mismatch")
    violations = [
        {"archive_sha256": entry["sha256"], "reason": entry["reason"]}
        for entry in entries
        if entry["status"] != "allow"
    ]
    if any(read_input(path) != body for path, body in frozen.items()):
        raise LicenseEvidenceError("license_inputs_changed")
    return {
        "spec_version": "harnessix.license-scan/v2",
        "scope": "all_locked_distribution_archives",
        "lock_sha256": digest(frozen[lock_path]),
        "project_sha256": digest(frozen[project_path]),
        "policy_sha256": digest(canonical_bytes(policy)),
        "evidence_index_sha256": digest(frozen[index_path]),
        "root": {
            "name": project["name"],
            "version": project["version"],
            "source": {"editable": "."},
            "license": project["license"],
            "license_file_sha256": digest(frozen[license_path]),
        },
        "package_count": len({archive.name for archive in archives}) + 1,
        "archive_count": len(entries),
        "violation_count": len(violations),
        "entries": entries,
        "violations": violations,
        "archive_reextraction_performed": False,
        "legal_rights_or_notice_completeness_claimed": False,
    }


def build_report(
    lock_path: Path,
    policy: dict,
    *,
    project_path: Path | None = None,
    evidence_directory: Path | None = None,
) -> dict:
    """完整证据返回确定性报告；缺失/畸形只抛固定未完成码，绝不读取本机元数据。"""

    try:
        return _build_report(
            lock_path, policy, project_path=project_path, evidence_directory=evidence_directory
        )
    except LicenseEvidenceError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise LicenseEvidenceError("license_evidence_invalid") from None


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="锁定发行物许可证据离线门禁")
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--evidence", type=Path, default=Path(EVIDENCE_DIRECTORY))
    parser.add_argument("--policy", type=Path, default=Path(POLICY_FILENAME))
    parser.add_argument("--output", type=Path, default=Path(REPORT_FILENAME))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.lock,
            read_json(args.policy),
            project_path=args.project,
            evidence_directory=args.evidence,
        )
        body = canonical_bytes(report)
        if args.check:
            if not args.output.is_file() or read_input(args.output) != body:
                print("许可证报告缺失或漂移：请显式重新生成并评审", file=sys.stderr)
                return 1
        else:
            write_atomic(args.output, body)
        if report["violation_count"]:
            print(f"许可证违规：{report['violation_count']}个Archive", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("许可证扫描未完成：license_cancelled", file=sys.stderr)
        return 2
    except LicenseEvidenceError as error:
        print(f"许可证扫描未完成：{error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("许可证扫描未完成：license_evidence_invalid", file=sys.stderr)
        return 2
    print(f"许可证扫描通过：{report['package_count']}个包、{report['archive_count']}个锁定Archive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
