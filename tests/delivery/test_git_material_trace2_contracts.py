"""显式 Trace2 固定目录与规范材料合同；不执行 Git 或授予批准。"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from harnessix.delivery.git_command import fixed_git_environment
from harnessix.delivery.git_material_input_contracts import (
    MAX_MANIFEST_BYTES,
    MAX_MATERIAL_BYTES,
    GitControlFileBinding,
    GitMaterialInput,
    GitMaterialInputError,
    decode_manifest,
    encode_manifest,
)

_LEGACY_KEYS = (
    "nonce source_digest stage_root stage_root_identity body_ref body_path body_sha256 "
    "body_bytes expected_oid object_format repo_path repo_identity common_path common_identity "
    "objects_path objects_identity control_files git_argv git_environment git_executable_identity "
    "expiry_monotonic_ns implementation_digest purpose_digest object_type version purpose"
).split()


def _profile():
    return importlib.import_module("harnessix.delivery.git_material_trace2_profile")


def _options(tmp_path: Path) -> dict:
    common = tmp_path / "repo" / ".git"
    stage = tmp_path / "stage"
    nonce = "a" * 64
    return {
        "nonce": nonce,
        "source_digest": "b" * 64,
        "stage_root": str(stage),
        "stage_root_identity": "c" * 64,
        "body_ref": f"body-{nonce}.bin",
        "body_path": str(stage / f"body-{nonce}.bin"),
        "body_sha256": hashlib.sha256(b"").hexdigest(),
        "body_bytes": 0,
        "expected_oid": "a" * 40,
        "object_format": "sha1",
        "repo_path": str(common.parent),
        "repo_identity": "d" * 64,
        "common_path": str(common),
        "common_identity": "e" * 64,
        "objects_path": str(common / "objects"),
        "objects_identity": "f" * 64,
        "control_files": (GitControlFileBinding(str(common / "config"), "a" * 64, 0, "b" * 64),),
        "git_argv": (str(tmp_path / "git"),),
        "git_environment": (("HOME", str(tmp_path / "home")),),
        "git_executable_identity": "a" * 64,
        "expiry_monotonic_ns": 123456,
        "implementation_digest": "b" * 64,
    }


def _json(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("ascii")


def _diagnostic(tmp_path: Path) -> GitMaterialInput:
    profile = _profile()
    options = _options(tmp_path)
    options.update(
        version="harnessix.git-material-input/v2",
        trace2_mode="stderr-event-v1",
        trace2_profile_sha256=profile.TRACE2_PROFILE_SHA256,
        git_environment=(("GIT_TRACE2_EVENT", "2"), ("HOME", str(tmp_path / "home"))),
    )
    return GitMaterialInput.create(**options)


def test_legacy_manifest_full_bytes_and_purpose_remain_unchanged(tmp_path: Path) -> None:
    request = GitMaterialInput.create(**_options(tmp_path))
    value = request.binding()
    assert set(value) == set(_LEGACY_KEYS)
    original = dict(value)
    original.pop("purpose_digest")
    assert request.purpose_digest == hashlib.sha256(_json(original)).hexdigest()
    assert encode_manifest(request) == _json(value)
    assert decode_manifest(_json(value)) == request


@pytest.mark.parametrize(
    "key", ["GIT_TRACE2_EVENT", "git_trace2_event", "GIT_TRACE", "GIT_TRACE_PACKET"]
)
def test_v1_rejects_unapproved_trace_environment_before_wire(tmp_path: Path, key: str) -> None:
    options = _options(tmp_path)
    options["git_environment"] = ((key, "2"),)
    with pytest.raises(GitMaterialInputError):
        GitMaterialInput.create(**options)


def test_explicit_off_environment_matches_original_closed_table(tmp_path: Path) -> None:
    values = (tmp_path / "git", tmp_path / "home", tmp_path / "tmp", ("file",), None)
    assert fixed_git_environment(*values, trace2_mode="off", trace2_profile_sha256="") == (
        fixed_git_environment(*values)
    )


def test_diagnostic_environment_has_exactly_one_new_key(tmp_path: Path) -> None:
    profile = _profile()
    values = (tmp_path / "git", tmp_path / "home", tmp_path / "tmp", ("file",), None)
    normal = fixed_git_environment(*values)
    diagnostic = fixed_git_environment(
        *values, trace2_mode="stderr-event-v1", trace2_profile_sha256=profile.TRACE2_PROFILE_SHA256
    )
    assert diagnostic == {**normal, "GIT_TRACE2_EVENT": "2"}


@pytest.mark.parametrize("mode", [None, True, 2, "2", "on", "STDERR-EVENT-V1"])
def test_mode_requires_actual_closed_string(mode) -> None:
    profile = _profile()
    with pytest.raises(ValueError, match="^git_material_trace2_invalid$"):
        profile.validate_material_trace2(mode, profile.TRACE2_PROFILE_SHA256)


@pytest.mark.parametrize("mode", ["off", "stderr-event-v1"])
@pytest.mark.parametrize("sha", [None, True, 0, "0" * 64, "profile"])
def test_mode_profile_pair_is_exact(mode: str, sha) -> None:
    with pytest.raises(ValueError):
        _profile().validate_material_trace2(mode, sha)


def test_profile_is_complete_canonical_and_deeply_readonly() -> None:
    p = _profile()
    assert p.TRACE2_PROFILE_ID == "harnessix.git-material-trace2-profile/v1"
    assert (p.TRACE2_EXPECTED_EVT, p.TRACE2_EXPECTED_EXE) == ("4", "2.55.0.windows.5")
    body = p.trace2_profile_bytes()
    assert hashlib.sha256(body).hexdigest() == p.TRACE2_PROFILE_SHA256
    assert _json(json.loads(body)) == body
    assert set(p.TRACE2_EVENT_SCHEMAS) == set(
        (
            "too_many_files version start exit signal atexit error cmd_path cmd_ancestry "
            "cmd_name cmd_mode alias child_start child_exit child_ready thread_start thread_exit "
            "exec exec_result def_param def_repo region_enter region_leave data data_json printf "
            "timer th_timer counter th_counter"
        ).split()
    )
    with pytest.raises(TypeError):
        p.TRACE2_EVENT_SCHEMAS["extra"] = p.TRACE2_EVENT_SCHEMAS["start"]
    with pytest.raises(TypeError):
        p.TRACE2_EVENT_SCHEMAS["start"].required["argv"] = "json"
    with pytest.raises(FrozenInstanceError):
        p.TRACE2_EVENT_SCHEMAS["start"].source.path = "other.c"
    assert p.TRACE2_EVENT_SCHEMAS["data"].required["value"] == "string"
    assert p.TRACE2_EVENT_SCHEMAS["data_json"].required["value"] == "json"
    assert p.TRACE2_EVENT_SCHEMAS["child_start"].required["use_shell"] == "boolean"
    assert "repo" not in p.TRACE2_EVENT_SCHEMAS["version"].optional
    assert p.TRACE2_EVENT_SCHEMAS["def_repo"].required["repo"] == "integer"
    assert p.TRACE2_EVENT_SCHEMAS["error"].optional["fmt"] == "string"
    for schema in p.TRACE2_EVENT_SCHEMAS.values():
        assert not set(schema.required) & set(schema.optional)
        assert schema.source.tag == "v2.55.0.windows.5"
        assert (
            schema.source.sha256
            == "d176fd3f7264f691c295091fa2ea3d045f383bbb0bc34d2774180636ca313de1"
        )


def test_error_formats_are_exact_static_source_identities() -> None:
    p = _profile()
    fmt = p.TRACE2_ERROR_FORMATS["Unable to add %s to database"]
    assert fmt.fixed_id == "HASH_OBJECT_ADD_AGGREGATE"
    assert fmt.sources[0].path == "builtin/hash-object.c"
    assert (
        fmt.sources[0].sha256 == "d5b371cce3ad59dc2aedda43f462ada6a0a9a50153f18b08711e58edd341bcf6"
    )
    assert p.TRACE2_ERROR_FORMATS["%s"].fixed_id == "GENERIC_DYNAMIC_FORMAT"
    assert "Unable to add secret to database" not in p.TRACE2_ERROR_FORMATS
    assert "unable to write loose object file: Permission denied" not in p.TRACE2_ERROR_FORMATS


def test_diagnostic_manifest_roundtrip_complete_and_not_v1(tmp_path: Path) -> None:
    request = _diagnostic(tmp_path)
    value = request.binding()
    assert set(value) == set(_LEGACY_KEYS) | {"trace2_mode", "trace2_profile_sha256"}
    assert value["trace2_mode"] == "stderr-event-v1"
    assert value["trace2_profile_sha256"] == _profile().TRACE2_PROFILE_SHA256
    assert decode_manifest(encode_manifest(request)) == request
    original = dict(value)
    original.pop("purpose_digest")
    assert request.purpose_digest == hashlib.sha256(_json(original)).hexdigest()


@pytest.mark.parametrize(
    "attack",
    [
        "v1-extra",
        "v2-missing-mode",
        "v2-missing-profile",
        "v2-off",
        "v2-bad-profile",
        "extra",
        "duplicate",
        "noncanonical",
    ],
)
def test_wire_rejects_cross_version_and_noncanonical_fields(tmp_path: Path, attack: str) -> None:
    value = _diagnostic(tmp_path).binding()
    if attack == "v1-extra":
        value["version"] = "harnessix.git-material-input/v1"
    elif attack.startswith("v2-missing"):
        del value["trace2_mode" if attack.endswith("mode") else "trace2_profile_sha256"]
    elif attack == "v2-off":
        value["trace2_mode"] = "off"
    elif attack == "v2-bad-profile":
        value["trace2_profile_sha256"] = "0" * 64
    elif attack == "extra":
        value["extra"] = True
    body = _json(value)
    if attack == "duplicate":
        body = body[:-2] + b',"trace2_mode":"stderr-event-v1"}\n'
    elif attack == "noncanonical":
        body += b"\n"
    with pytest.raises(GitMaterialInputError):
        decode_manifest(body)


@pytest.mark.parametrize("value", ["0", "1", "true", "3", "", "/tmp/trace", "2\n"])
def test_v2_requires_exact_existing_stderr_target(tmp_path: Path, value: str) -> None:
    request = _diagnostic(tmp_path)
    options = request.binding()
    options.pop("purpose_digest")
    options["control_files"] = request.control_files
    options["git_environment"] = (("GIT_TRACE2_EVENT", value),)
    with pytest.raises(GitMaterialInputError):
        GitMaterialInput.create(**options)


def test_material_and_manifest_caps_are_not_reduced(tmp_path: Path) -> None:
    assert MAX_MATERIAL_BYTES == 8 * 1024 * 1024
    assert MAX_MANIFEST_BYTES == 64 * 1024
    options = _options(tmp_path)
    options["body_bytes"] = MAX_MATERIAL_BYTES
    assert GitMaterialInput.create(**options).body_bytes == MAX_MATERIAL_BYTES
    options["body_bytes"] += 1
    with pytest.raises(GitMaterialInputError):
        GitMaterialInput.create(**options)


def test_diagnostic_extra_trace_key_and_case_duplicate_rejected(tmp_path: Path) -> None:
    request = _diagnostic(tmp_path)
    for key in ("GIT_TRACE2_EVENT_BRIEF", "GIT_TRACE", "git_trace2_event"):
        options = request.binding()
        options.pop("purpose_digest")
        options["control_files"] = request.control_files
        options["git_environment"] = tuple(sorted((*request.git_environment, (key, "2"))))
        with pytest.raises(GitMaterialInputError):
            GitMaterialInput.create(**options)


def test_profile_environment_rejects_non_actual_string_keys_and_values() -> None:
    p = _profile()
    for environment in ({1: "2"}, {"GIT_TRACE2_EVENT": 2}, {b"GIT_TRACE2_EVENT": "2"}):
        with pytest.raises(ValueError, match="^git_material_trace2_invalid$"):
            p.validate_material_trace2_environment(
                "stderr-event-v1", p.TRACE2_PROFILE_SHA256, environment
            )


def test_off_command_digest_matches_original_formula(tmp_path: Path) -> None:
    from harnessix.delivery.git_command import GitCommand
    from harnessix.execution.contracts import canonical_digest

    command = GitCommand(
        tmp_path,
        "a" * 64,
        ("status",),
        (str(tmp_path / "git"), "status"),
        (("HOME", str(tmp_path / "home")),),
        "b" * 64,
        b"body",
        None,
        (0,),
        20.0,
        ("file",),
    )
    expected = {
        "version": "git-fixed-command/v1",
        "cwd": __import__("os").path.normcase(str(tmp_path)),
        "cwd_identity": "a" * 64,
        "executable_identity": "b" * 64,
        "argv": command.argv,
        "environment": command.environment,
        "input_present": True,
        "input_bytes": 4,
        "input_sha256": hashlib.sha256(b"body").hexdigest(),
        "accepted": (0,),
        "timeout_seconds": 20.0,
        "allowed_protocols": ("file",),
    }
    assert command.digest == canonical_digest(expected)


@pytest.mark.parametrize("field", ["trace2_mode", "trace2_profile_sha256"])
def test_frozen_request_changed_trace_fields_rejected(tmp_path: Path, field: str) -> None:
    request = _diagnostic(tmp_path)
    object.__setattr__(request, field, "off" if field == "trace2_mode" else "0" * 64)
    with pytest.raises(ValueError):
        encode_manifest(request)


def test_profile_real_source_bytes_enter_both_original_implementation_digests(monkeypatch) -> None:
    from harnessix.delivery.git import git_delivery_implementation_digest
    from harnessix.delivery.git_material_input_contracts import implementation_digest

    original = Path.read_bytes
    before = implementation_digest(), git_delivery_implementation_digest()
    reads = []

    def observed(path):
        raw = original(path)
        if path.name == "git_material_trace2_profile.py":
            reads.append(path)
            return raw + b"\n"
        return raw

    monkeypatch.setattr(Path, "read_bytes", observed)
    assert (implementation_digest(), git_delivery_implementation_digest()) != before
    assert len(reads) == 2


@pytest.mark.parametrize("field", ["trace2_mode", "trace2_profile_sha256", "environment"])
def test_command_missing_new_fields_is_fixed_rejection(tmp_path: Path, field: str) -> None:
    from harnessix.delivery.git_command import GitCommand

    command = GitCommand(
        tmp_path,
        "a" * 64,
        ("status",),
        (str(tmp_path / "git"), "status"),
        (("HOME", str(tmp_path / "home")),),
        "b" * 64,
        None,
        None,
        (0,),
        20.0,
        ("file",),
    )
    object.__delattr__(command, field)
    with pytest.raises(ValueError, match="^git_material_trace2_invalid$"):
        command.__post_init__()
