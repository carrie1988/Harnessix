"""Freeze the run config of one Bailian deepseek-v4-pro-0813 run (no model calls; run from the source root).

Usage: prepare-bailian.py <tag> <public plan directory> <v39|v37> <fee stop amount>
v39 = v37 + every-step thinking; v37 = the current default workflow without thinking. Both use the Bailian idle
prices and billing window, profile v9, 100 requests / 2M tokens / 2700 s."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from harnessix.evals.execution_fs import ensure_private_directory
from harnessix.evals.swebench.io import read_preregistration_plan
from harnessix.evals.swebench.public_test_profile import discover_swebench_case_process_profiles
from harnessix.evals.swebench.run_config import build_swebench_agent_run_config
from harnessix.evals.swebench.run_config_io import read_swebench_agent_run_config, write_swebench_agent_run_config
from harnessix.evals.swebench.run_settings import MODEL_CATALOG
from harnessix.evals.swebench.source_revision import require_harnessix_execution_revision

TAG, PUBLIC, VERSION, FEE_STOP = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
assert VERSION in {"v39", "v37"}
SOURCE = Path("/Users/zhangjinhui/.codex/worktrees/r3-workflow-efficiency/Harnessix")
BASE = Path("/Users/zhangjinhui/Library/Application Support/Harnessix/swebench")
ENGINE = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
MODEL = "deepseek-v4-pro-0813"
plan = read_preregistration_plan(SOURCE / PUBLIC / "plan.json")
v46 = read_preregistration_plan(SOURCE / "benchmarks/swebench/generalization-25-v46/plan.json")
old = read_swebench_agent_run_config(BASE / f"runs/agent-{v46.plan_sha256[:12]}-v46/run-config.json")
require_harnessix_execution_revision(SOURCE, Path(old.git_executable), plan.harnessix_revision)
thinking = "enabled" if VERSION == "v39" else "disabled"
assert plan.model_name == MODEL_CATALOG[MODEL].public_name
assert {p.name: p.value for p in plan.model_settings.parameters} == {"parallel_tool_calls": True, "thinking": thinking}
audit = SOURCE / PUBLIC / "exclusion-audit.json"
if audit.exists():
    excluded = set(json.loads(audit.read_text())["excluded_instance_ids"])
    assert len(excluded) == 325 and not {c.instance_id for c in plan.cases} & excluded
profiles = discover_swebench_case_process_profiles(plan, ENGINE, writable_snapshot=True, require_native_selectors=True)
root = BASE / "runs" / f"agent-{plan.plan_sha256[:12]}-{TAG}"
if root.exists():
    raise SystemExit("run root exists")
ensure_private_directory(root, error_code="run_root_invalid", label=f"百炼DeepSeek {TAG}")
config = build_swebench_agent_run_config(
    plan, source_root=SOURCE, work_root=root,
    cache_root=BASE / "generalization-v45/public-repository-cache",
    git_executable=Path(old.git_executable), case_process_profiles=profiles,
    fee_stop_amount=FEE_STOP, api_key_env="DASHSCOPE_API_KEY", model=MODEL,
    observed_at=datetime.now(UTC), isolate_provider_failures=True, budget_pressure_compaction=False,
    recycle_workspaces=True, working_set_result_groups=None, compact_model_surface=True,
    convergence_guidance=True, patch_deadline_requests=4, budget_capped_truncation=True,
    prompt_cache=True, cached_closure_workflow=True, scratch_reproduction=True, protocol_robust=True,
    explained_gates=True, connect_retry=True, forced_choice_tolerant=True, argument_repair=True,
    test_timeout_failures=True, reads_after_failed_patch=True, truncation_quality=True, self_verification=True,
    bounded_repair=True, clean_review_notice=True, provider_proxy_url="http://127.0.0.1:7890", repair_closure=True,
    sibling_scope=True, wide_output=True, full_thinking=VERSION == "v39",
)
config.require_plan(plan)
provider = config.provider_config
expected_spec = "harnessix.swebench-agent-run-config/" + VERSION
assert config.spec_version == expected_spec and config.full_thinking is (VERSION == "v39")
assert provider.model == MODEL and provider.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
assert provider.enable_thinking is (VERSION == "v39") and provider.thinking_budget is None
assert provider.max_output_tokens == (16384 if VERSION == "v39" else 8192)
assert provider.prompt_cache == "stable_tail" and provider.implicit_cache_usage
assert provider.proxy_url == "http://127.0.0.1:7890" and provider.capabilities.parallel_tool_calls
assert config.billing_window is not None and config.price.output_per_million == "13.5"
assert all(p.profile.version == "swebench-public-tests-v9" for p in config.case_process_profiles)
write_swebench_agent_run_config(root / "run-config.json", config)
print(json.dumps({"plan_sha256": plan.plan_sha256, "root": str(root), "spec": config.spec_version,
                  "fingerprint": config.fingerprint, "fee_stop": config.fee_stop_amount,
                  "worst_case_all_cases": config.worst_case_amount(plan)}, ensure_ascii=False))
