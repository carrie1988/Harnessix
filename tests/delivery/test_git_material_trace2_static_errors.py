"""三个静态错误模板的封闭合同；仅使用合成数据，不认定原生成功或唯一根因。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from harnessix.delivery import git_material_trace2_profile as profile
from tests.product_config import git_trace2_projection as projection

_TAG = "v2.55.0.windows.5"
_OBJECT_SHA = "d79dbe915c778016f896ddf61dcc3ff48b75d5356856a97c06f36017afc406cc"
_SETUP_SHA = "3151e42f4893a9034a41103a1c23491181d8d79b80c75dea4d88ce24f626d611"
_HASH_SHA = "d5b371cce3ad59dc2aedda43f462ada6a0a9a50153f18b08711e58edd341bcf6"
_BASE_SOURCE_SHA = "f0cac9ca79e9d38d54d8b38ecfed5ccd25f0abb2b3347536fe18c5ef1bf63259"
_BASE_PROFILE_SHA = "7120ca7cc6d73275bd29980b3b7f66be5ff0608d02ec1b241b86505677ea6e48"
_NEW = (
    ("short read while indexing %s", "OBJECT_INDEX_SHORT_READ", 1086, 1087),
    (
        "insufficient permission for adding an object to repository database %s",
        "OBJECT_DATABASE_ADD_PERMISSION",
        672,
        674,
    ),
    ("unable to set permission to '%s'", "OBJECT_FINALIZE_PERMISSION", 469, 470),
)
_LEGACY = (
    (
        "not a git repository: '%s'",
        "SETUP_EXPLICIT_NOT_REPOSITORY",
        "setup.c",
        _SETUP_SHA,
        1171,
        1171,
    ),
    ("'$%s' too big", "SETUP_GITDIR_ENV_BOUND", "setup.c", _SETUP_SHA, 1157, 1157),
    (
        "Unable to add %s to database",
        "HASH_OBJECT_ADD_AGGREGATE",
        "builtin/hash-object.c",
        _HASH_SHA,
        31,
        33,
    ),
    (
        "Unable to hash %s",
        "HASH_OBJECT_HASH_AGGREGATE",
        "builtin/hash-object.c",
        _HASH_SHA,
        31,
        33,
    ),
    (
        "refusing to create malformed object",
        "OBJECT_FORMAT_MALFORMED",
        "object-file.c",
        _OBJECT_SHA,
        1014,
        1014,
    ),
    ("%s", "GENERIC_DYNAMIC_FORMAT", "setup.c", _SETUP_SHA, 781, 781),
)
_CANARY = "SYNTHETIC_PRIVATE_VALUE"
_ARGV = tuple(f"synthetic-arg-{index}" for index in range(22))
_FIELDS = {
    "profile_status",
    "completeness",
    "reason",
    "stage_witnesses",
    "error_format_ids",
    "return_consistency",
    "source_profile_id",
}


def _event(event_name: str, **fields) -> dict:
    return {
        "event": event_name,
        "sid": "synthetic-sid",
        "thread": "main",
        "time": _CANARY,
        **fields,
    }


def _events(*formats: str) -> list[dict]:
    # 全部字段均为固定合成值，不读取现场 fmt、msg、raw、path 或 errno。
    return [
        _event("version", evt="4", exe=profile.TRACE2_EXPECTED_EXE),
        _event("start", t_abs=0.001, argv=list(_ARGV)),
        _event("cmd_name", name="hash-object"),
        *(_event("error", fmt=literal, msg=_CANARY) for literal in formats),
        _event("exit", t_abs=0.002, code=128),
        _event("atexit", t_abs=0.003, code=128),
    ]


def _project(events: list[dict], *, expected_sha: str | None = None) -> dict:
    body = b"".join(json.dumps(event).encode("ascii") + b"\n" for event in events)
    result = projection.project_git_trace2_events(
        body,
        _ARGV,
        profile.TRACE2_PROFILE_SHA256 if expected_sha is None else expected_sha,
        git_returncode=128,
    )
    assert set(result) == _FIELDS
    assert _CANARY not in json.dumps(result)
    return result


@pytest.mark.parametrize("literal,fixed_id,start,end", _NEW)
def test_exact_static_template_has_fixed_enum_and_verified_source(literal, fixed_id, start, end):
    entry = profile.TRACE2_ERROR_FORMATS.get(literal)
    assert entry is not None
    assert entry.fixed_id == fixed_id
    assert [asdict(source) for source in entry.sources] == [
        {
            "tag": _TAG,
            "path": "object-file.c",
            "sha256": _OBJECT_SHA,
            "line_start": start,
            "line_end": end,
        }
    ]


@pytest.mark.parametrize("literal,fixed_id,start,end", _NEW)
def test_exact_static_template_projects_only_finite_enum(literal, fixed_id, start, end):
    result = _project(_events(literal))
    assert result["error_format_ids"] == [fixed_id]
    assert result["completeness"] == "KNOWN" and result["reason"] == "NONE"
    assert result["return_consistency"] == "MATCHED_128"


@pytest.mark.parametrize("literal,fixed_id,start,end", _NEW)
@pytest.mark.parametrize(
    "mutation",
    [
        "prefix",
        "suffix",
        "leading-space",
        "trailing-space",
        "newline",
        "expanded",
        "escaped-percent",
        "integer-placeholder",
        "positional-placeholder",
        "missing-placeholder",
        "case",
        "errno-text-tail",
        "errno-placeholder-tail",
        "embedded-nul",
    ],
)
def test_near_static_templates_remain_unknown(literal, fixed_id, start, end, mutation):
    altered = {
        "prefix": "error: " + literal,
        "suffix": literal + " extra",
        "leading-space": " " + literal,
        "trailing-space": literal + " ",
        "newline": literal + "\n",
        "expanded": literal.replace("%s", _CANARY),
        "escaped-percent": literal.replace("%s", "%%s"),
        "integer-placeholder": literal.replace("%s", "%d"),
        "positional-placeholder": literal.replace("%s", "%1$s"),
        "missing-placeholder": literal.replace("%s", ""),
        "case": literal.upper(),
        "errno-text-tail": literal + ": SYNTHETIC_ERRNO_TEXT",
        "errno-placeholder-tail": literal + ": %s",
        "embedded-nul": literal + "\0",
    }[mutation]
    assert altered not in profile.TRACE2_ERROR_FORMATS
    result = _project(_events(altered))
    assert result["error_format_ids"] == []
    assert result["completeness"] == "UNKNOWN" and result["reason"] == "UNCLASSIFIED_FORMAT"


@pytest.mark.parametrize(
    "literal",
    [
        "read error while indexing %s",
        "unable to create temporary file",
        "unable to get random bytes for temporary file",
        "unable to write file %s",
        "unable to write loose object file",
        "error when closing loose object file",
        "fsync error on '%s'",
        "mmap failed%s",
    ],
)
@pytest.mark.parametrize("tail", ["", ": %s", ": SYNTHETIC_ERRNO_TEXT"])
def test_errno_families_are_not_added_as_static_or_wide_matches(literal, tail):
    result = _project(_events(literal + tail))
    assert result["completeness"] == "UNKNOWN" and result["reason"] == "UNCLASSIFIED_FORMAT"
    assert result["error_format_ids"] == []


@pytest.mark.parametrize("literal,fixed_id,path,sha,start,end", _LEGACY)
def test_all_legacy_enums_and_source_metadata_remain_unchanged(
    literal, fixed_id, path, sha, start, end
):
    entry = profile.TRACE2_ERROR_FORMATS[literal]
    assert entry.fixed_id == fixed_id
    assert [asdict(source) for source in entry.sources] == [
        {"tag": _TAG, "path": path, "sha256": sha, "line_start": start, "line_end": end}
    ]
    assert _project(_events(literal))["error_format_ids"] == [fixed_id]


def test_closed_catalog_contains_only_legacy_and_three_new_static_templates():
    assert set(profile.TRACE2_ERROR_FORMATS) == {row[0] for row in (*_LEGACY, *_NEW)}
    assert len({entry.fixed_id for entry in profile.TRACE2_ERROR_FORMATS.values()}) == 9


def test_new_ids_can_coexist_with_aggregate_without_exporting_caller_or_root_cause():
    literals = [row[0] for row in _NEW]
    result = _project(_events(*literals, literals[0], "Unable to add %s to database"))
    assert result["error_format_ids"] == sorted(
        [row[1] for row in _NEW] + ["HASH_OBJECT_ADD_AGGREGATE"]
    )
    assert result["completeness"] == "KNOWN" and result["return_consistency"] == "MATCHED_128"
    # 非穷举集合不携带顺序、caller、errno、proof 或成功信息。
    assert result == _project(_events("Unable to add %s to database", *reversed(literals)))


@pytest.mark.parametrize("literal,fixed_id,start,end", _NEW)
@pytest.mark.parametrize(
    "boundary",
    [
        "frozen-profile",
        "missing-version",
        "wrong-version",
        "wrong-exe",
        "wrong-sid",
        "wrong-argv",
        "extra-key",
        "terminal-mismatch",
    ],
)
def test_new_static_formats_do_not_bypass_original_safety_preconditions(
    literal,
    fixed_id,
    start,
    end,
    boundary,
):
    events = _events(literal)
    expected_sha = None
    if boundary == "frozen-profile":
        expected_sha = _BASE_PROFILE_SHA
    elif boundary == "missing-version":
        del events[0]
    elif boundary == "wrong-version":
        events[0]["evt"] = "5"
    elif boundary == "wrong-exe":
        events[0]["exe"] = "2.55.0"
    elif boundary == "wrong-sid":
        events[3]["sid"] = "other-synthetic-sid"
    elif boundary == "wrong-argv":
        events[1]["argv"][0] = "other-synthetic-arg"
    elif boundary == "extra-key":
        events[3]["unapproved"] = _CANARY
    else:
        events[-1]["code"] = 0
    assert _project(events, expected_sha=expected_sha)["completeness"] == "UNKNOWN"


def test_profile_source_digest_changes_with_static_catalog():
    assert hashlib.sha256(Path(profile.__file__).read_bytes()).hexdigest() != _BASE_SOURCE_SHA


def test_canonical_profile_digest_binds_new_catalog_and_rejects_frozen_identity():
    body = profile.trace2_profile_bytes()
    assert hashlib.sha256(body).hexdigest() == profile.TRACE2_PROFILE_SHA256 != _BASE_PROFILE_SHA
    profile.validate_material_trace2("stderr-event-v1", profile.TRACE2_PROFILE_SHA256)
    with pytest.raises(ValueError, match="^git_material_trace2_invalid$"):
        profile.validate_material_trace2("stderr-event-v1", _BASE_PROFILE_SHA)
    profile.validate_material_trace2_environment(
        "stderr-event-v1", profile.TRACE2_PROFILE_SHA256, {"GIT_TRACE2_EVENT": "2"}
    )
    profile.validate_material_trace2_environment("off", "", {})
    rules = json.loads(body)["rules"]
    assert rules["unknown"] == "fail_unknown"
    assert rules["dynamic_format"] == "unclassified_no_errno"
    assert rules["source_reporter"] == "not_original_fatal_stack"
    assert rules["error_sources"] == "known_examples_non_exhaustive_not_unique_callers"
    assert rules["source_identity"] == "not_binary_build_attestation"
