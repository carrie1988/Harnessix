"""Archive级元数据解释与保守许可策略；未知声明不得借包名覆盖。"""

from __future__ import annotations

import re
from email import policy as email_policy
from email.parser import Parser
from pathlib import PurePosixPath

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.utils import canonicalize_name
from packaging.version import Version

from scripts.license_contracts import LicenseEvidenceError, LockedArchive

_VERSIONS = {"1.0", "1.1", "1.2", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6"}
_LEGACY = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "3-Clause BSD License": "BSD-3-Clause",
    "BSD-3-Clause": "BSD-3-Clause",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}


def _path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise LicenseEvidenceError("license_notice_path_invalid")
    return path


def declared_notice_members(
    body: bytes, kind: str, metadata_member: str, available: set[str]
) -> list[str]:
    """2.4起严格使用licenses目录；旧Wheel仅在两个历史位置恰有一处时兼容。"""

    try:
        message = Parser(policy=email_policy.compat32).parsestr(body.decode("utf-8"))
        version = message.get("Metadata-Version")
        if version not in _VERSIONS:
            raise LicenseEvidenceError("license_metadata_invalid")
        root = _path(metadata_member).parts[0]
        result = []
        for value in message.get_all("License-File", []):
            relative = _path(value)
            prefixes = [PurePosixPath(root)]
            if kind == "wheel":
                prefixes = [PurePosixPath(root) / "licenses"]
                if Version(version) < Version("2.4"):
                    prefixes.append(PurePosixPath(root))
            matches = [
                str(prefix / relative) for prefix in prefixes if str(prefix / relative) in available
            ]
            if len(matches) != 1:
                raise LicenseEvidenceError("license_declared_notice_missing")
            result.append(matches[0])
    except LicenseEvidenceError:
        raise
    except (UnicodeError, ValueError, TypeError):
        raise LicenseEvidenceError("license_metadata_invalid") from None
    return result


def metadata_facts(
    body: bytes, archive: LockedArchive, metadata_member: str, notices: list[dict]
) -> dict:
    """先验证单值身份与License-File，再解释本文件声明；不借用相邻Wheel结论。"""

    try:
        message = Parser(policy=email_policy.compat32).parsestr(body.decode("utf-8"))
        if message.defects:
            raise LicenseEvidenceError("license_metadata_invalid")
        for header in ("Metadata-Version", "Name", "Version", "License", "License-Expression"):
            if len(message.get_all(header, [])) > 1:
                raise LicenseEvidenceError("license_metadata_duplicate_field")
        version, name, package_version = (
            message.get(key) for key in ("Metadata-Version", "Name", "Version")
        )
        if version not in _VERSIONS or not name or not package_version:
            raise LicenseEvidenceError("license_metadata_invalid")
        if canonicalize_name(name, validate=True) != archive.name or Version(
            package_version
        ) != Version(archive.version):
            raise LicenseEvidenceError("license_metadata_identity_mismatch")
        expression, legacy = message.get("License-Expression"), message.get("License")
        if expression is not None and (Version(version) < Version("2.4") or legacy is not None):
            raise LicenseEvidenceError("license_metadata_conflicting_fields")
        notice_names = {record["member"] for record in notices}
        declared_files = message.get_all("License-File", [])
        if len(set(declared_files)) != len(declared_files):
            raise LicenseEvidenceError("license_metadata_duplicate_field")
        declared_notice_members(body, archive.kind, metadata_member, notice_names)
        classifiers = [
            value.split("::")[-1].strip()
            for value in message.get_all("Classifier", [])
            if value.startswith("License ::")
        ]
        if expression is not None:
            declared, source = expression.strip(), "license_expression"
        elif legacy and legacy.strip() in _LEGACY:
            declared, source = _LEGACY[legacy.strip()], "legacy_exact_alias"
        elif legacy and "\n" not in legacy.strip() and len(legacy.strip()) <= 512:
            declared, source = legacy.strip(), "legacy_license_field"
        elif not legacy and len(set(classifiers)) == 1 and classifiers[0] in _LEGACY:
            declared, source = _LEGACY[classifiers[0]], "legacy_unambiguous_classifier"
        else:
            declared, source = "UNKNOWN", "review_required"
    except LicenseEvidenceError:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise LicenseEvidenceError("license_metadata_invalid") from None
    return {
        "metadata_version": version,
        "declared_license": declared,
        "declaration_source": source,
        "declared_license_files": declared_files,
    }


def _validate_reviews(reviews: list) -> None:
    """收据必须使用精确身份合同，拒绝名称级或未知字段覆盖。"""

    for review in reviews:
        if (
            not isinstance(review, dict)
            or set(review) != {"review_id", "expression", "basis", "binding"}
            or not isinstance(review["review_id"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", review["review_id"])
            or not isinstance(review["basis"], str)
            or not 1 <= len(review["basis"]) <= 4096
            or not isinstance(review["expression"], str)
            or len(review["expression"]) > 512
            or not isinstance(review["binding"], dict)
            or set(review["binding"])
            != {
                "name",
                "version",
                "registry",
                "kind",
                "url",
                "sha256",
                "size_bytes",
                "metadata_sha256",
                "notice_sha256",
            }
        ):
            raise LicenseEvidenceError("license_review_invalid")


def validate_policy(policy: dict) -> None:
    """白名单为明确SPDX原子；WITH例外必须单独登记，不允许名称级overrides。"""

    if (
        policy.get("spec_version") != "harnessix.license-policy/v2"
        or set(policy) - {"spec_version", "review", "allow", "deny", "allow_exceptions", "reviews"}
        or not isinstance(policy.get("reviews", []), list)
    ):
        raise LicenseEvidenceError("license_policy_invalid")
    _validate_reviews(policy.get("reviews", []))
    for key in ("allow", "deny"):
        values = policy.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(v, str) for v in values)
            or len(set(values)) != len(values)
        ):
            raise LicenseEvidenceError("license_policy_invalid")
        for value in values:
            try:
                if re.search(r"[ ()]", value) or canonicalize_license_expression(value) != value:
                    raise LicenseEvidenceError("license_policy_invalid")
            except InvalidLicenseExpression:
                raise LicenseEvidenceError("license_policy_invalid") from None
    if set(policy["allow"]) & set(policy["deny"]):
        raise LicenseEvidenceError("license_policy_invalid")
    exceptions = policy.get("allow_exceptions", [])
    if (
        not isinstance(exceptions, list)
        or any(not isinstance(v, str) for v in exceptions)
        or len(set(exceptions)) != len(exceptions)
    ):
        raise LicenseEvidenceError("license_policy_invalid")
    for expression in exceptions:
        try:
            if (
                canonicalize_license_expression(expression) != expression
                or " WITH " not in expression
                or " AND " in expression
                or " OR " in expression
            ):
                raise LicenseEvidenceError("license_policy_invalid")
        except InvalidLicenseExpression:
            raise LicenseEvidenceError("license_policy_invalid") from None


def decide(expression: str, policy: dict) -> tuple[str, str, str | None]:
    """AND/OR都要求所有原子获准；不自动选择OR分支来绕过拒绝策略。"""

    try:
        canonical = canonicalize_license_expression(expression)
    except InvalidLicenseExpression:
        return "violation", "license_expression_unknown", None
    tokens = re.sub(r"[()]", " ", canonical).split()
    atoms, exceptions = [], []
    for index, token in enumerate(tokens):
        if token in {"AND", "OR", "WITH"} or (index and tokens[index - 1] == "WITH"):
            continue
        atoms.append(token)
        if index + 2 < len(tokens) and tokens[index + 1] == "WITH":
            exceptions.append(f"{token} WITH {tokens[index + 2]}")
    if any(atom in policy["deny"] for atom in atoms):
        return "violation", "license_expression_denied", canonical
    if not atoms or any(atom not in policy["allow"] for atom in atoms):
        return "violation", "license_expression_not_allowed", canonical
    if any(pair not in policy.get("allow_exceptions", []) for pair in exceptions):
        return "violation", "license_exception_not_allowed", canonical
    return "allow", "license_expression_allowed", canonical


def notice_signals(body: bytes, policy: dict) -> list[str]:
    """许可证正文标题只是复核信号，不自动推导完整SPDX义务或项目法律结论。"""

    signals = []
    for family, title in (
        ("LGPL-", b"GNU LESSER GENERAL PUBLIC LICENSE"),
        ("GPL-", b"GNU GENERAL PUBLIC LICENSE"),
    ):
        if any(value.startswith(family) for value in policy["deny"]) and re.search(
            rb"(?m)^[ \t]*" + title + rb"[ \t\r]*$", body
        ):
            signals.append("license_notice_restricted_title")
    return signals


def reviewed_expression(record: dict, policy: dict, facts: dict) -> tuple[str, str, str | None]:
    """人工解释也必须匹配完整Archive/元数据/通知摘要；旧版本收据不能移植。"""

    identity = {
        key: record[key]
        for key in ("name", "version", "registry", "kind", "url", "sha256", "size_bytes")
    }
    binding = {
        **identity,
        "metadata_sha256": record["metadata"]["sha256"],
        "notice_sha256": sorted(item["sha256"] for item in record["notices"]),
    }
    matches = [review for review in policy.get("reviews", []) if review.get("binding") == binding]
    if len(matches) > 1:
        raise LicenseEvidenceError("license_review_duplicate")
    if not matches:
        return facts["declared_license"], facts["declaration_source"], None
    review = matches[0]
    if (
        facts["declaration_source"] == "license_expression"
        or not review.get("basis")
        or not review.get("review_id")
    ):
        raise LicenseEvidenceError("license_review_invalid")
    return review["expression"], "archive_bound_review", review["review_id"]
