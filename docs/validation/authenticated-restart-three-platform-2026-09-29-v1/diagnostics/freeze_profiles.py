"""从预注册认证基线一次性封印Profile；不读取候选或改写业务状态。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
ARCHIVE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakManifestV5
from scripts.soak_threshold import (
    SoakThresholdProfile,
    _margin_upper,
    publish_profile,
    read_profile,
    verify_profile_baseline,
)

SOURCE = "7cbe358ff0f22ea2bc0813478bb1c13a2b1e7c46"
PLAN_PATH = "docs/changes/m09-r1-authenticated-restart-baseline-plan.json"
PLAN_SHA = "7ee884c39078cc9ed71f6dda78bbbe4777179351b76d58aeb277e68bb7ee9c0a"


def main() -> None:
    # 已封印目录只读；再次执行不得先创建第二个Profile再发现结果文件已存在。
    assert not (ARCHIVE / "profiles").exists()
    assert not (ARCHIVE / "facts/profile-freeze.json").exists()
    plan_bytes = subprocess.check_output(["git", "show", f"{SOURCE}:{PLAN_PATH}"], cwd=ROOT)
    assert sha256(plan_bytes).hexdigest() == PLAN_SHA
    assert (ROOT / PLAN_PATH).read_bytes() == plan_bytes
    plan = json.loads(plan_bytes)
    tree = subprocess.check_output(["git", "rev-parse", f"{SOURCE}:src"], cwd=ROOT, text=True).strip()
    assert tree == plan["product_tree_oid"]
    for path, expected in plan["source_inputs_sha256"].items():
        body = subprocess.check_output(["git", "show", f"{SOURCE}:{path}"], cwd=ROOT)
        assert sha256(body).hexdigest() == expected
    observation = json.loads((ARCHIVE / "facts/baseline-workflow.json").read_bytes())
    assert observation["headSha"] == SOURCE and observation["conclusion"] == "success"
    assert observation["status"] == "completed" and len(observation["jobs"]) == 3
    assert all(job["conclusion"] == "success" for job in observation["jobs"])
    assert datetime.fromisoformat(observation["createdAt"]) > datetime.fromisoformat(plan["registered_at"])
    results = []
    for platform, policy in plan["platform_policies"].items():
        profile_path = ROOT / plan["original_archive"] / "profiles" / platform / policy["original_profile_id"]
        original, digest = read_profile(profile_path)
        assert digest == policy["original_profile_sha256"]
        runs = list((ARCHIVE / "raw" / platform).glob("*/manifest.json"))
        assert len(runs) == 1
        baseline_path = runs[0].parent
        baseline, manifest_sha = read_published_run(baseline_path)
        assert isinstance(baseline, SoakManifestV5) and baseline.status == "baseline"
        assert baseline.code_revision == SOURCE and baseline.platform == platform
        assert baseline.load.model_dump(mode="json") == plan["load"]
        assert baseline.sample_counts == plan["sample_counts"]
        assert baseline.fault_counts.model_dump(mode="json") == plan["expected_fault_counts"]
        assert baseline.provider.script_version == plan["provider_script_version"]
        assert baseline.provider.request_count == 0
        data = original.model_dump()
        data.update(profile_id=uuid4().hex, hardware_class=baseline.hardware_class,
                    baseline_run_id=baseline.run_id, baseline_manifest_sha256=manifest_sha,
                    created_at=datetime.now(UTC))
        for metric, limit in data["metric_limits"].items():
            assert limit["margin_basis_points"] == policy["metric_margin_basis_points"][metric]
            for percentile in ("p50", "p95", "p99"):
                limit[f"{percentile}_upper"] = _margin_upper(
                    getattr(baseline.statistics[metric], percentile), limit["margin_basis_points"]
                )
        for kind, limit in data["growth_limits"].items():
            assert limit["margin_basis_points"] == policy["growth_margin_basis_points"][kind]
            growth = max(0, getattr(baseline.file_watermarks, f"{kind}_after_bytes")
                         - getattr(baseline.file_watermarks, f"{kind}_before_bytes"))
            limit["upper_bytes"] = _margin_upper(growth, limit["margin_basis_points"])
        profile = SoakThresholdProfile.model_validate(data)
        verify_profile_baseline(profile, baseline_path)
        directory, profile_sha = publish_profile(ARCHIVE / "profiles" / platform, profile, baseline_path)
        assert read_profile(directory) == (profile, profile_sha)
        results.append({"platform": platform, "baseline_run_id": baseline.run_id,
                        "profile_id": profile.profile_id, "profile_sha256": profile_sha,
                        "baseline_manifest_sha256": manifest_sha,
                        "db_growth_bytes": baseline.file_watermarks.db_after_bytes,
                        "db_upper_bytes": profile.growth_limits["db"].upper_bytes,
                        "startup_p95_ns": baseline.statistics["product_startup"].p95,
                        "rss_peak_bytes": baseline.statistics["rss_peak"].p95})
    facts = {"baseline_set": plan["baseline_set"], "source_revision": SOURCE,
             "plan_sha256": PLAN_SHA, "old_failures_preserved": True,
             "thresholds_from_candidate": False, "profiles": results,
             "paid_model_requests": 0, "engineering_trials_counted": 0}
    target = ARCHIVE / "facts/profile-freeze.json"
    with target.open("x") as stream:
        stream.write(json.dumps(facts, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(facts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
