"""只读重算认证重启原件、分位数及原余量，结果不修改冻结文件。"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ARCHIVE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_profile, verify_profile_baseline


def main() -> None:
    plan = json.loads((ROOT / "docs/changes/m09-r1-authenticated-restart-baseline-plan.json").read_bytes())
    frozen = json.loads((ARCHIVE / "facts/profile-freeze.json").read_bytes())
    checks = []
    for item in frozen["profiles"]:
        platform = item["platform"]
        directory = ARCHIVE / "raw" / platform / item["baseline_run_id"]
        manifest, digest = read_published_run(directory)
        profile, profile_sha = read_profile(ARCHIVE / "profiles" / platform / item["profile_id"])
        assert verify_profile_baseline(profile, directory) == (manifest, digest)
        assert digest == item["baseline_manifest_sha256"] and profile_sha == item["profile_sha256"]
        assert manifest.code_revision == frozen["source_revision"]
        assert manifest.load.model_dump(mode="json") == plan["load"]
        assert manifest.sample_counts == plan["sample_counts"]
        assert manifest.fault_counts.model_dump(mode="json") == plan["expected_fault_counts"]
        started, final = read_attempt(directory.parent / "attempts" / manifest.run_id)
        assert final is not None and final.outcome == "committed" and final.manifest_sha256 == digest
        assert started.code_revision == manifest.code_revision
        samples = [json.loads(line) for line in (directory / "samples.jsonl").read_text().splitlines()]
        for metric, statistics in manifest.statistics.items():
            observed = sorted(s["value"] for s in samples if s["metric"] == metric and s["phase"] == "measure")
            assert len(observed) == plan["sample_counts"][metric]
            margin = plan["platform_policies"][platform]["metric_margin_basis_points"][metric]
            assert profile.metric_limits[metric].margin_basis_points == margin
            for percentile, rank in (("p50", .50), ("p95", .95), ("p99", .99)):
                value = observed[math.ceil(len(observed) * rank) - 1]
                assert value == getattr(statistics, percentile)
                assert (value * (10000 + margin) + 9999) // 10000 == getattr(profile.metric_limits[metric], f"{percentile}_upper")
        for kind, limit in profile.growth_limits.items():
            before = getattr(manifest.file_watermarks, f"{kind}_before_bytes")
            after = getattr(manifest.file_watermarks, f"{kind}_after_bytes")
            margin = plan["platform_policies"][platform]["growth_margin_basis_points"][kind]
            assert limit.margin_basis_points == margin
            assert limit.upper_bytes == (max(0, after - before) * (10000 + margin) + 9999) // 10000
        proof = json.loads((directory / "restart-proof.json").read_bytes())
        assert proof["thread_count"] == 500 and proof["hard_exit_ack"] and proof["hard_exit_eof"]
        assert len(proof["cycles"]) == 5
        assert [c["owner_generation"] for c in proof["cycles"]] == [1, 2, 3, 4, 5]
        assert all(c["thread_count"] == 500 and c["thread_set_sha256"] == proof["thread_set_sha256"] for c in proof["cycles"])
        assert datetime.fromisoformat(profile.created_at.isoformat()) > final.finished_at
        checks.append({"platform": platform, "complete_run_and_attempt": True,
                       "raw_quantiles_recomputed": True, "original_margins_recomputed": True,
                       "five_cycles_and_full_thread_set": True, "profile_frozen_after_baseline": True})
    print(json.dumps({"status": "PASS", "scope": "baseline_and_profile_only", "checks": checks,
                      "candidate_executed": False, "commercial_release_accepted": False,
                      "paid_model_requests": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
