"""SWE-bench 25 Case 预注册、prediction 导出与官方评测 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from harnessix.agent.errors import KernelError
from harnessix.evals.execution_fs import ensure_private_directory, path_present
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchPreRegistrationPlan,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.execution import run_swebench_agent_suite
from harnessix.evals.swebench.io import (
    read_dataset_jsonl,
    read_preregistration_plan,
    write_preregistration_plan,
)
from harnessix.evals.swebench.official import (
    build_official_evaluation_command,
    import_official_result,
    run_official_evaluation,
)
from harnessix.evals.swebench.predictions import (
    SweBenchPrediction,
    collect_workspace_patch,
    read_predictions,
    write_predictions,
)
from harnessix.evals.swebench.public_test_profile import (
    discover_swebench_case_process_profiles,
)
from harnessix.evals.swebench.run_config import build_swebench_agent_run_config
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)

_RUN_CONFIG_FILE = "run-config.json"


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, "coding-eval-swebench 参数无效；使用 --help 查看格式。\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(
        prog="harnessix coding-eval-swebench",
        description="SWE-bench 外部基准适配器；预注册与真实评测结果严格分离",
        allow_abbrev=False,
    )
    subcommands = parser.add_subparsers(dest="operation", required=True)

    preregister = subcommands.add_parser("preregister", allow_abbrev=False)
    preregister.add_argument("--dataset-jsonl", required=True)
    preregister.add_argument("--output", required=True)
    preregister.add_argument("--dataset-name", required=True)
    preregister.add_argument("--dataset-revision", required=True)
    preregister.add_argument("--dataset-artifact-sha256", required=True)
    preregister.add_argument("--split", default="test")
    preregister.add_argument("--harnessix-revision", required=True)
    preregister.add_argument("--swebench-revision", required=True)
    preregister.add_argument("--swebench-package-version", required=True)
    preregister.add_argument("--task-repo-revision", required=True)
    preregister.add_argument("--model-name", required=True)
    preregister.add_argument("--provider", required=True)
    preregister.add_argument("--model-parameter", action="append", default=[])
    preregister.add_argument("--salt", required=True)
    preregister.add_argument("--max-steps", type=int, required=True)
    preregister.add_argument("--max-tokens", type=int, required=True)
    preregister.add_argument("--timeout-seconds", type=float, required=True)
    preregister.add_argument("--max-output-chars", type=int, required=True)
    preregister.add_argument("--max-tool-calls-per-step", type=int, required=True)

    validate = subcommands.add_parser("validate", allow_abbrev=False)
    validate.add_argument("--plan", required=True)

    run_agent = subcommands.add_parser("run-agent", allow_abbrev=False)
    run_agent.add_argument("--plan", required=True)
    run_agent.add_argument("--source-root", required=True)
    run_agent.add_argument("--work-root", required=True)
    run_agent.add_argument("--cache-root", required=True)
    run_agent.add_argument("--git", required=True)
    run_agent.add_argument("--container-engine", required=True)
    run_agent.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    run_agent.add_argument("--fee-stop-amount", default="80")
    run_agent.add_argument("--continue-isolated-provider-failures", action="store_true")
    run_agent.add_argument("--budget-pressure-compaction", action="store_true")
    run_agent.add_argument("--allow-repository-network", action="store_true")
    run_agent.add_argument("--allow-provider-network", action="store_true")

    export = subcommands.add_parser("export-predictions", allow_abbrev=False)
    export.add_argument("--plan", required=True)
    export.add_argument("--workspaces", required=True)
    export.add_argument("--git", required=True)
    export.add_argument("--output", required=True)

    evaluate = subcommands.add_parser("evaluate", allow_abbrev=False)
    evaluate.add_argument("--plan", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--swebench", required=True)
    evaluate.add_argument("--swebench-source", required=True)
    evaluate.add_argument("--git", required=True)
    evaluate.add_argument("--work-root", required=True)
    evaluate.add_argument("--workers", type=int, default=4)
    evaluate.add_argument("--timeout-seconds", type=int, default=1800)
    evaluate.add_argument("--task-repo", required=True)
    evaluate.add_argument("--allow-network", action="store_true")

    verify = subcommands.add_parser("verify-results", allow_abbrev=False)
    verify.add_argument("--plan", required=True)
    verify.add_argument("--predictions", required=True)
    verify.add_argument("--work-root", required=True)
    return parser


def _model_parameters(values: list[str]) -> tuple[SweBenchModelParameter, ...]:
    parameters: list[SweBenchModelParameter] = []
    for item in values:
        name, separator, value = item.partition("=")
        if not separator:
            raise ValueError
        parameters.append(SweBenchModelParameter(name=name, value=value))
    return tuple(sorted(parameters, key=lambda parameter: parameter.name))


def _preregister(arguments: argparse.Namespace) -> dict[str, object]:
    plan = build_swebench_preregistration_plan(
        read_dataset_jsonl(Path(arguments.dataset_jsonl)),
        dataset_name=arguments.dataset_name,
        dataset_revision=arguments.dataset_revision,
        dataset_artifact_sha256=arguments.dataset_artifact_sha256,
        split=arguments.split,
        harnessix_revision=arguments.harnessix_revision,
        swebench_revision=arguments.swebench_revision,
        swebench_package_version=arguments.swebench_package_version,
        task_repo_revision=arguments.task_repo_revision,
        model_name=arguments.model_name,
        model_config=SweBenchModelConfig(
            provider=arguments.provider,
            parameters=_model_parameters(arguments.model_parameter),
        ),
        task_budget=SweBenchTaskBudget(
            max_steps=arguments.max_steps,
            max_tokens=arguments.max_tokens,
            timeout_seconds=arguments.timeout_seconds,
            max_output_chars=arguments.max_output_chars,
            max_tool_calls_per_step=arguments.max_tool_calls_per_step,
        ),
        salt=arguments.salt,
    )
    write_preregistration_plan(Path(arguments.output), plan)
    return {
        "reason": "completed",
        "registered_cases": len(plan.cases),
        "plan_sha256": plan.plan_sha256,
    }


def _export_predictions(arguments: argparse.Namespace) -> dict[str, object]:
    plan = read_preregistration_plan(Path(arguments.plan))
    root = Path(arguments.workspaces).resolve(strict=True)
    predictions = tuple(
        SweBenchPrediction(
            instance_id=case.instance_id,
            model_name_or_path=plan.model_name,
            model_patch=collect_workspace_patch(
                root / case.instance_id,
                Path(arguments.git),
                case.base_commit,
            ),
        )
        for case in plan.cases
    )
    revision = write_predictions(Path(arguments.output), plan, predictions)
    return {
        "reason": "completed",
        "exported_cases": len(predictions),
        "predictions_sha256": revision,
    }


def _run_agent(arguments: argparse.Namespace) -> dict[str, object]:
    if not arguments.allow_provider_network:
        raise KernelError("swebench_network_not_enabled", "SWE-bench Provider联网未启用")
    plan = read_preregistration_plan(Path(arguments.plan))
    source_root = Path(arguments.source_root).resolve(strict=True)
    git_executable = Path(arguments.git).resolve(strict=True)
    container_engine = Path(arguments.container_engine).resolve(strict=True)
    work_root = Path(arguments.work_root).resolve(strict=False)
    cache_root = Path(arguments.cache_root).resolve(strict=False)
    ensure_private_directory(
        work_root,
        error_code="swebench_execution_root_invalid",
        label="SWE-bench私有运行目录",
    )
    config_path = work_root / _RUN_CONFIG_FILE
    if path_present(config_path):
        config = read_swebench_agent_run_config(config_path)
        if (
            config.plan_sha256 != plan.plan_sha256
            or config.source_root != str(source_root)
            or config.work_root != str(work_root)
            or config.cache_root != str(cache_root)
            or config.git_executable != str(git_executable)
            or any(
                binding.profile.container_engine != str(container_engine)
                for binding in config.case_process_profiles
            )
            or config.provider_config.api_key_env != arguments.api_key_env
            or config.fee_stop_amount != arguments.fee_stop_amount
            or config.isolate_provider_failures != arguments.continue_isolated_provider_failures
            or config.budget_pressure_compaction != arguments.budget_pressure_compaction
        ):
            raise KernelError("swebench_run_config_invalid", "SWE-bench既有执行配置与参数不一致")
        try:
            config.require_plan(plan)
        except ValueError:
            raise KernelError(
                "swebench_run_config_invalid", "SWE-bench既有执行配置与计划不一致"
            ) from None
    else:
        case_process_profiles = discover_swebench_case_process_profiles(plan, container_engine)
        config = build_swebench_agent_run_config(
            plan,
            source_root=source_root,
            work_root=work_root,
            cache_root=cache_root,
            git_executable=git_executable,
            case_process_profiles=case_process_profiles,
            fee_stop_amount=arguments.fee_stop_amount,
            api_key_env=arguments.api_key_env,
            isolate_provider_failures=arguments.continue_isolated_provider_failures,
            budget_pressure_compaction=arguments.budget_pressure_compaction,
        )
        write_swebench_agent_run_config(config_path, config)
    api_key = os.environ.get(config.provider_config.api_key_env, "")
    return asyncio.run(
        run_swebench_agent_suite(
            plan,
            config,
            api_key=api_key,
            allow_repository_network=arguments.allow_repository_network,
            allow_provider_network=True,
        )
    )


def _evaluate(arguments: argparse.Namespace) -> dict[str, object]:
    plan = read_preregistration_plan(Path(arguments.plan))
    predictions = read_predictions(Path(arguments.predictions), plan)
    command = build_official_evaluation_command(
        executable=Path(arguments.swebench),
        swebench_source=Path(arguments.swebench_source),
        git_executable=Path(arguments.git),
        plan=plan,
        predictions_path=Path(arguments.predictions),
        predictions=predictions,
        workers=arguments.workers,
        timeout_seconds=arguments.timeout_seconds,
        task_repo=Path(arguments.task_repo),
    )
    run_official_evaluation(
        command=command,
        work_root=Path(arguments.work_root),
        allow_network=arguments.allow_network,
    )
    return _verified_summary(plan, predictions, Path(arguments.work_root))


def _verified_summary(
    plan: SweBenchPreRegistrationPlan,
    predictions: tuple[SweBenchPrediction, ...],
    work_root: Path,
) -> dict[str, object]:
    _, summary = import_official_result(
        work_root=work_root,
        plan=plan,
        predictions=predictions,
    )
    return {"reason": "completed", **summary.model_dump(mode="json", warnings="error")}


def main(argv: Sequence[str]) -> None:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.operation == "preregister":
            result = _preregister(arguments)
        elif arguments.operation == "validate":
            plan = read_preregistration_plan(Path(arguments.plan))
            result = {
                "reason": "completed",
                "registered_cases": len(plan.cases),
                "plan_sha256": plan.plan_sha256,
            }
        elif arguments.operation == "run-agent":
            result = _run_agent(arguments)
        elif arguments.operation == "export-predictions":
            result = _export_predictions(arguments)
        elif arguments.operation == "evaluate":
            if not arguments.allow_network:
                raise KernelError(
                    "swebench_network_not_enabled",
                    "SWE-bench 官方评测未显式允许联网",
                )
            result = _evaluate(arguments)
        else:
            plan = read_preregistration_plan(Path(arguments.plan))
            predictions = read_predictions(Path(arguments.predictions), plan)
            result = _verified_summary(plan, predictions, Path(arguments.work_root))
    except (KernelError, OSError, ValueError):
        print(json.dumps({"reason": "failed"}, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
