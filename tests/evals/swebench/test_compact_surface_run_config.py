"""工作集＋紧凑模型表面独立冻结为v14；任务、模型、费用边界不变，只与工作集组合。"""

import json

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.evals.swebench.run_config import (
    SweBenchCompactSurfaceAgentRunConfig,
    SweBenchWorkingSetAgentRunConfig,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.product_config.agent_context import build_product_agent_context
from tests.evals.swebench.test_run_config import build_agent_test_plan
from tests.evals.swebench.test_working_set_run_config import working_set_config
from tests.product_config.test_agent_context import _request


def compact_config(tmp_path, **changes):
    return working_set_config(tmp_path, **{"compact_model_surface": True, **changes})


def test_compact_surface_v14_roundtrips_without_changing_task_model_or_fee_budget(tmp_path):
    config = compact_config(tmp_path, working_set_result_groups=1)
    working_set = working_set_config(tmp_path, working_set_result_groups=1)
    assert type(config) is SweBenchCompactSurfaceAgentRunConfig
    assert type(working_set) is SweBenchWorkingSetAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v14"
    assert config.compact_model_surface and not working_set.compact_model_surface
    assert config.working_set_result_groups == 1
    assert config.isolate_provider_failures and config.budget_pressure_compaction
    assert config.workspace_retirement_enabled
    assert config.provider_config == working_set.provider_config
    assert (
        config.price == working_set.price and config.fee_stop_amount == working_set.fee_stop_amount
    )
    plan = build_agent_test_plan()
    assert config.worst_case_amount(plan) == working_set.worst_case_amount(plan)
    assert config.fingerprint != working_set.fingerprint
    path = tmp_path / "compact.json"
    write_swebench_agent_run_config(path, config)
    restored = read_swebench_agent_run_config(path)
    assert type(restored) is SweBenchCompactSurfaceAgentRunConfig and restored == config


@pytest.mark.parametrize(
    "changes",
    [
        {"working_set_result_groups": None},
        {"compact_model_surface": 1},
        {"compact_model_surface": None},
        {"enable_definition_views": True},
        {"enable_regex_search": True},
        {"reserve_closing_phase": True},
        {"enable_planning": True},
        {"enable_thinking": True},
        {"recycle_workspaces": False},
    ],
)
def test_compact_surface_rejects_standalone_or_mixed_candidates(tmp_path, changes):
    with pytest.raises((ValueError, ValidationError)):
        compact_config(tmp_path, **changes)


async def test_compact_instructions_replace_only_the_post_patch_coding_fragment(tmp_path):
    async def prepare(**settings):
        return await build_product_agent_context(
            tmp_path, require_pre_edit_profile=False, **settings
        ).context.prepare(_request(tmp_path), CancelToken())

    standard = await prepare()
    assert standard == await prepare(compact_instructions=False)
    compact = await prepare(compact_instructions=True)
    fragments = json.loads(compact.instructions)["fragments"]
    runtime = [f for f in fragments if f["kind"] == "runtime_instruction"]
    assert [f["source"] for f in runtime] == ["harnessix.coding-instructions/v26"]
    for rule in ("run_profile", "process_nonzero_exit", "weaken tests", "git_review", "secrets"):
        assert rule in runtime[0]["content"]
    assert len(compact.instructions) < len(standard.instructions)


@pytest.mark.parametrize(
    "changes",
    [
        {"require_pre_edit_profile": True},
        {"allow_optional_pre_edit_profile": True},
        {"enable_regex_search": True},
        {"enable_definition_views": True},
        {"enable_grep_snapshots": True},
        {"reserve_closing_phase": True},
        {"enable_planning": True},
        {"compact_instructions": 1},
    ],
)
def test_compact_instructions_reject_other_instruction_variants(tmp_path, changes):
    arguments = {"require_pre_edit_profile": False, "compact_instructions": True, **changes}
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, **arguments)


def test_convergence_v15_adds_only_guidance_policy_to_v14(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchConvergenceAgentRunConfig

    config = compact_config(tmp_path, convergence_guidance=True)
    compact = compact_config(tmp_path)
    assert type(config) is SweBenchConvergenceAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v15"
    assert config.convergence_guidance and not compact.convergence_guidance
    assert config.compact_model_surface and config.working_set_result_groups == 2
    excluded = {"spec_version", "convergence_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == compact.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v15.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"compact_model_surface": False},
        {"working_set_result_groups": None},
        {"convergence_guidance": 1},
    ],
)
def test_convergence_requires_the_full_compact_working_set_stack(tmp_path, changes):
    with pytest.raises((ValueError, ValidationError)):
        compact_config(tmp_path, **{"convergence_guidance": True, **changes})


async def test_convergence_fragment_requires_compact_instructions(tmp_path):
    with pytest.raises(ValueError):
        build_product_agent_context(
            tmp_path, require_pre_edit_profile=False, convergence_guidance=True
        )
    prepared = await build_product_agent_context(
        tmp_path,
        require_pre_edit_profile=False,
        compact_instructions=True,
        convergence_guidance=True,
    ).context.prepare(_request(tmp_path), CancelToken())
    sources = [f["source"] for f in json.loads(prepared.instructions)["fragments"]]
    assert "harnessix.coding-convergence/v1" in sources


def test_patch_deadline_v16_adds_only_deadline_policy_to_v15(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchPatchDeadlineAgentRunConfig

    config = compact_config(tmp_path, convergence_guidance=True, patch_deadline_requests=4)
    converge = compact_config(tmp_path, convergence_guidance=True)
    assert type(config) is SweBenchPatchDeadlineAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v16"
    assert config.patch_deadline == 4 and converge.patch_deadline is None
    excluded = {"spec_version", "patch_deadline_requests"}
    assert config.model_dump(mode="json", exclude=excluded) == converge.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v16.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [
        {"convergence_guidance": False},
        {"patch_deadline_requests": 0},
        {"patch_deadline_requests": 17},
        {"patch_deadline_requests": True},
    ],
)
def test_patch_deadline_requires_convergence_stack_and_valid_count(tmp_path, changes):
    with pytest.raises((ValueError, ValidationError)):
        compact_config(
            tmp_path, **{"convergence_guidance": True, "patch_deadline_requests": 4, **changes}
        )


def test_budget_truncation_v17_adds_only_truncation_policy_to_v16(tmp_path):
    from harnessix.evals.swebench.run_config import SweBenchBudgetTruncationAgentRunConfig

    deadline = {"convergence_guidance": True, "patch_deadline_requests": 4}
    config = compact_config(tmp_path, **deadline, budget_capped_truncation=True)
    v16 = compact_config(tmp_path, **deadline)
    assert type(config) is SweBenchBudgetTruncationAgentRunConfig
    assert config.spec_version == "harnessix.swebench-agent-run-config/v17"
    assert config.budget_capped_truncation and not v16.budget_capped_truncation
    excluded = {"spec_version", "truncation_policy"}
    assert config.model_dump(mode="json", exclude=excluded) == v16.model_dump(
        mode="json", exclude=excluded
    )
    path = tmp_path / "v17.json"
    write_swebench_agent_run_config(path, config)
    assert read_swebench_agent_run_config(path) == config


@pytest.mark.parametrize(
    "changes",
    [{"patch_deadline_requests": None}, {"budget_capped_truncation": 1}],
)
def test_budget_truncation_requires_the_patch_deadline_stack(tmp_path, changes):
    with pytest.raises((ValueError, ValidationError)):
        compact_config(
            tmp_path,
            **{
                "convergence_guidance": True,
                "patch_deadline_requests": 4,
                "budget_capped_truncation": True,
                **changes,
            },
        )


@pytest.mark.parametrize("enabled", [True, False])
async def test_reproduction_fragment_is_explicit_and_compact_only(tmp_path, enabled):
    prepared = await build_product_agent_context(
        tmp_path,
        require_pre_edit_profile=False,
        compact_instructions=True,
        convergence_guidance=True,
        reproduction_guidance=enabled,
    ).context.prepare(_request(tmp_path), CancelToken())
    sources = [f["source"] for f in json.loads(prepared.instructions)["fragments"]]
    assert ("harnessix.coding-reproduction/v1" in sources) is enabled
    with pytest.raises(ValueError):
        build_product_agent_context(
            tmp_path, require_pre_edit_profile=False, reproduction_guidance=True
        )
