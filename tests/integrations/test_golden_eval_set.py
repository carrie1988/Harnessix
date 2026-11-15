"""黄金评测集第1版的清单与锁定题目一致：任何一处改动都要另建新版本（docs/operations/golden-eval-set.md）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "benchmarks/harbor/golden-v1/golden.json"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parts() -> list[dict]:
    return manifest()["parts"]


def locked_names(part: dict) -> list[str]:
    lock = json.loads((ROOT / part["lock_file"]).read_text(encoding="utf-8"))
    return [task["name"] for task in lock["tasks"]]


def test_the_manifest_is_version_one_with_two_parts():
    data = manifest()
    assert data["spec_version"] == "harnessix.golden-eval-set/v1"
    assert (data["name"], data["version"], data["frozen_on"]) == (
        "harnessix-golden",
        1,
        "2026-11-14",
    )
    assert [part["part_id"] for part in data["parts"]] == [
        "multilingual-test28",
        "verified-dev23",
    ]


@pytest.mark.parametrize("part", parts(), ids=lambda part: part["part_id"])
def test_locked_files_are_unchanged(part: dict):
    for key in ("lock", "selection"):
        content = (ROOT / part[f"{key}_file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == part[f"{key}_sha256"]


@pytest.mark.parametrize("part", parts(), ids=lambda part: part["part_id"])
def test_valid_tasks_are_the_locked_tasks_minus_the_exclusions(part: dict):
    names = locked_names(part)
    excluded = [entry["task"] for entry in part["excluded"]]
    assert len(names) == len(set(names)) == part["locked_tasks"]
    assert set(excluded) <= set(names) and len(excluded) == len(set(excluded))
    assert all(entry["reason"].strip() for entry in part["excluded"])
    assert len(names) - len(excluded) == part["valid_tasks"]
    lock = json.loads((ROOT / part["lock_file"]).read_text(encoding="utf-8"))
    assert {task["git_commit_id"] for task in lock["tasks"]} == {part["harbor_datasets_commit"]}


def test_baselines_fit_their_part():
    by_id = {part["part_id"]: part for part in parts()}
    for baseline in manifest()["baselines"]:
        part = by_id[baseline["part_id"]]
        assert 0 <= baseline["resolved"] <= baseline["valid_slots"]
        assert baseline["valid_slots"] <= part["valid_tasks"] * part["attempts_per_system"]
        assert baseline["mean_minutes"] > 0


def test_every_part_has_an_exposure_record():
    logged = {entry["part_id"] for entry in manifest()["exposure_log"]}
    assert logged == {part["part_id"] for part in parts()}
