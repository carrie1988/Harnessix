#!/usr/bin/env python3
"""创建不含凭据值的工程Task Pack百炼真实Suite私有配置。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_suite import build_task_pack_suite_config
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot

_ROOT = Path(__file__).resolve().parents[1]
_PACK_ID = "harnessix-engineering"
_PACK_VERSION = 2
_MODEL = "qwen3-coder-plus-2025-09-23"
_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_PRICE_URL = "https://help.aliyun.com/zh/model-studio/qwen3-coder-plus"


def _executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError(f"缺少宿主程序：{name}")
    return Path(value).resolve(strict=True)


def _git_text(git: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (str(git), "--no-pager", *arguments),
        cwd=_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("无法读取Git事实")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _require_clean_revision(git: Path) -> str:
    revision = _git_text(git, "rev-parse", "HEAD")
    if _git_text(git, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("真实Provider基线要求干净且已提交的源码树")
    return revision


def _price(now: datetime) -> PriceSnapshot:
    end = now + timedelta(hours=24)
    return PriceSnapshot(
        version=f"aliyun-bailian-{now.date().isoformat()}-qwen3-coder-plus-32k",
        source_url=_PRICE_URL,
        billing_provider="aliyun-bailian",
        model=_MODEL,
        region="cn-beijing",
        service_tier="payg",
        inference_mode="non-thinking",
        currency="CNY",
        valid_from=now,
        valid_until=end,
        input_tokens_min=0,
        input_tokens_max=32_000,
        input_price=FlatInputPrice(per_million="4"),
        output_per_million="16",
    )


def _billing() -> BillingContext:
    return BillingContext(
        billing_provider="aliyun-bailian",
        region="cn-beijing",
        service_tier="payg",
        inference_mode="non-thinking",
    )


def _write_private(path: Path, config: CodingEvalProviderSuiteRunConfig) -> None:
    parent = path.parent.resolve(strict=True)
    target = parent / path.name
    if target.exists() or target.is_symlink():
        raise RuntimeError("配置目标已存在；恢复运行必须复用原配置")
    body = (config.model_dump_json(indent=2) + "\n").encode("utf-8")
    temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="创建工程Task Pack百炼真实Suite配置")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--suite-id", type=UUID)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--fee-stop-amount", default="40")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    git = _executable("git")
    container = _executable("docker")
    revision = _require_clean_revision(git)
    now = datetime.now(UTC)
    loaded = builtin_coding_eval_task_pack(_PACK_ID, _PACK_VERSION)
    suite = build_task_pack_suite_config(
        loaded,
        suite_id=arguments.suite_id or uuid4(),
        work_root=arguments.work_root.resolve(strict=False),
        environment=CodingEvalEnvironment(
            harnessix_revision=revision,
            provider="openai_chat",
            model=_MODEL,
            platform=sys.platform,
            isolation="fixed-container-checks-provider-network",
        ),
        price=_price(now),
        billing_context=_billing(),
        fee_stop_amount=arguments.fee_stop_amount,
        created_at=now,
    )
    config = CodingEvalProviderSuiteRunConfig(
        suite=suite,
        pack_id=loaded.manifest.pack_id,
        pack_version=loaded.manifest.pack_version,
        pack_sha256=loaded.manifest.pack_sha256,
        source_root=str(_ROOT),
        git_executable=str(git),
        container_engine=str(container),
        provider_config=OpenAIChatConfig(
            base_url=_BASE_URL,
            model=_MODEL,
            api_key_env=arguments.api_key_env,
            capabilities=ChatCapabilities(tool_calls=True, parallel_tool_calls=False),
            max_output_tokens=4096,
            timeout_seconds=900,
            io_timeout_seconds=60,
            max_attempts=1,
            retry_delay_seconds=0,
            max_request_bytes=4 * 1024 * 1024,
            max_response_bytes=4 * 1024 * 1024,
            output_token_parameter="max_tokens",
        ),
    )
    _write_private(arguments.output, config)
    print(
        json.dumps(
            {
                "suite_id": str(config.suite.plan.suite_id),
                "config_fingerprint": config.fingerprint,
                "scheduled_cases": len(config.suite.plan.cases),
                "scheduled_trials": sum(
                    len(campaign.run_ids) for campaign in config.suite.campaign_plans
                ),
                "fee_stop_currency": config.suite.fee_stop_currency,
                "fee_stop_amount": config.suite.fee_stop_amount,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
