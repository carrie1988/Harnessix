"""候选路径与实际文件绑定的离线回归；合成材料不作为官方或现场见证。"""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest

from scripts.windows_git_native_branch_observation import contract, observe, preflight
from tests.governance.test_windows_git_native_branch_observation import (
    synthetic_pair as synthetic_pair,
)


@pytest.fixture
def layout(tmp_path, monkeypatch, synthetic_pair):
    executable, symbols, expected = synthetic_pair
    root = tmp_path / "git-root"
    for relative in ("cmd/git.exe", "bin/git.exe", "mingw64/bin/git.exe"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(executable, target)
    repository = tmp_path / "repository"
    repository.mkdir()
    tool = tmp_path / "tool-placeholder"
    tool.write_bytes(b"not-executed")
    fixed = contract.read_contract()
    fixed["pairs"] = [dict(expected, role=role) for role in ("wrapper", "core")]
    monkeypatch.setattr(preflight, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(preflight, "source_checks", lambda *args: [])
    monkeypatch.setattr(preflight, "existing_tools", lambda *args: (tool, tool))
    monkeypatch.setattr(
        preflight, "download_symbols", lambda *args: dict.fromkeys(("wrapper", "core"), symbols)
    )
    monkeypatch.setattr(observe, "run_debugger", lambda *args: pytest.fail("禁止执行调试器"))
    return root, repository, tmp_path / "output", fixed, symbols, tool


@pytest.mark.parametrize("relative", ["cmd/git.exe", "mingw64/bin/git.exe", "bin/git.exe"])
def test_selected_file_and_roles_are_retained_through_real_pair_recheck(
    layout, monkeypatch, relative
):
    root, repository, output, fixed, _, _ = layout
    selected = root / relative
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    state = preflight.prepare(repository, output, fixed)
    assert state["selected"] == selected.resolve()
    assert state["git_paths"]["core"] == root / "mingw64/bin/git.exe"
    assert state["git_paths"]["wrapper"] == (
        selected if relative == "bin/git.exe" else root / "cmd/git.exe"
    )
    preflight.recheck(repository, state, fixed)
    assert (output / "bootstrap.cdb").is_file()


def test_bin_candidate_does_not_require_or_silently_substitute_cmd(layout, monkeypatch):
    root, repository, output, fixed, _, _ = layout
    (root / "cmd/git.exe").unlink()
    selected = root / "bin/git.exe"
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    state = preflight.prepare(repository, output, fixed)
    assert state["git_paths"]["wrapper"] == selected
    preflight.recheck(repository, state, fixed)


@pytest.mark.parametrize("relative", ["usr/bin/git.exe", "shims/git.exe", "ucrt64/bin/git.exe"])
def test_foreign_layout_is_not_accepted_by_wrapper_bytes(layout, monkeypatch, relative):
    root, repository, output, fixed, _, _ = layout
    selected = root / relative
    selected.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "bin/git.exe", selected)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    with pytest.raises(ValueError, match="selected_git_layout_unrecognized"):
        preflight.prepare(repository, output, fixed)
    assert not output.exists()


@pytest.mark.parametrize("directory", ["cmd", "bin"])
def test_non_git_exe_cannot_claim_a_role(layout, monkeypatch, directory):
    root, _, _, _, _, _ = layout
    selected = root / directory / "git.cmd"
    shutil.copyfile(root / "bin/git.exe", selected)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    with pytest.raises(ValueError, match="selected_git_not_verified_role"):
        preflight.selected_paths()


def test_bin_without_core_is_not_a_complete_layout(layout, monkeypatch):
    root, _, _, _, _, _ = layout
    (root / "mingw64/bin/git.exe").unlink()
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(root / "bin/git.exe"))
    with pytest.raises(ValueError, match="selected_git_layout_unrecognized"):
        preflight.selected_paths()


@pytest.mark.parametrize("damage,reason", [("byte", "sha_mismatch"), ("short", "size_mismatch")])
def test_bin_identity_failure_is_not_hidden_by_valid_cmd(layout, monkeypatch, damage, reason):
    root, repository, output, fixed, _, _ = layout
    selected = root / "bin/git.exe"
    body = selected.read_bytes()
    selected.write_bytes(body[:-1] + bytes([body[-1] ^ 1]) if damage == "byte" else body[:-1])
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    with pytest.raises(ValueError, match=reason):
        preflight.prepare(repository, output, fixed)
    assert not (output / "bootstrap.cdb").exists()


@pytest.mark.parametrize(
    "field,value,reason",
    [("guid", "00000000-0000-0000-0000-000000000000", "guid_age"), ("age", 2, "guid_age")],
)
def test_bin_full_codeview_contract_is_not_replaced_by_path(
    layout, monkeypatch, field, value, reason
):
    root, repository, output, fixed, _, _ = layout
    fixed["pairs"][0][field] = value
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(root / "bin/git.exe"))
    with pytest.raises(ValueError, match=reason):
        preflight.prepare(repository, output, fixed)


@pytest.mark.parametrize("damage", ["selected", "pe", "pdb", "tool", "source"])
def test_actual_recheck_refuses_drift_before_any_debugger(layout, monkeypatch, damage):
    root, repository, output, fixed, symbols, tool = layout
    selected = root / "bin/git.exe"
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    state = preflight.prepare(repository, output, fixed)
    if damage == "selected":
        monkeypatch.setattr(preflight.shutil, "which", lambda name: str(root / "cmd/git.exe"))
        reason = "selected_git_changed"
    elif damage in ("pe", "pdb"):
        target = selected if damage == "pe" else symbols
        body = target.read_bytes()
        target.write_bytes(body[:-1] + bytes([body[-1] ^ 1]))
        reason = "official_pair_sha_mismatch"
    elif damage == "tool":
        tool.write_bytes(b"changed")
        reason = "existing_tool_changed"
    else:

        def source_failure(*args):
            raise ValueError("current_source_drift")

        monkeypatch.setattr(preflight, "source_checks", source_failure)
        reason = "current_source_drift"
    with pytest.raises(ValueError, match=reason):
        preflight.recheck(repository, state, fixed)


@pytest.mark.parametrize("damage,reason", [("byte", "sha_mismatch"), ("short", "size_mismatch")])
def test_actual_refusal_remains_nonzero_without_exporting_selected_path(
    layout, monkeypatch, tmp_path, damage, reason
):
    root, repository, output, fixed, _, _ = layout
    selected = root / "bin/git.exe"
    body = selected.read_bytes()
    selected.write_bytes(body[:-1] + bytes([body[-1] ^ 1]) if damage == "byte" else body[:-1])
    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(selected))
    monkeypatch.setattr(observe, "read_contract", lambda: fixed)
    report = tmp_path / "report"
    assert observe.run(repository, output, report, False, "") == 2
    result = json.loads((report / "result.json").read_bytes())
    assert result["status"] == "PREFLIGHT_REFUSED" and not result["execution_performed"]
    assert result["preflight_diagnostic"]["first_failed_stage"] == "pe_pdb_identity"
    assert result["preflight_diagnostic"]["reason_code"] == "official_pair_" + reason
    assert not result["branch_gate_passed"] and not result["original_sdk_acceptance"]
    assert str(selected) not in json.dumps(result)
    assert {path.name for path in report.iterdir()} == {"result.json", "result-sha256.json"}
