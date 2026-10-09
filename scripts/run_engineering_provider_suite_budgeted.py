"""有限北京Coder验证宿主；复用正式Suite，仅增加持久请求预留与显式凭据注入。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import harnessix
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.cli_config import read_private_eval_config
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.provider_suite_execution import (
    _GIT_ENVIRONMENT,
    run_task_pack_provider_suite,
)
from harnessix.evals.provider_suite_execution import (
    _require_scope as _require_runtime_scope,
)
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunReport
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_publication import provider_publication_scope
from harnessix.models._provider_io import validate_key
from harnessix.models.contracts import ModelProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import FlatInputPrice
from harnessix.observability import Observability
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import (
    MODEL,
    PRICE_SOURCE,
    BailianVerificationBounds,
    GuardedVerificationProvider,
    VerificationRequestPacer,
)


def _require_source_checkout(config: CodingEvalProviderSuiteRunConfig) -> None:
    """只验证付费宿主准入时的来源和干净状态，不宣称运行期间源码不可变。"""
    try:
        root = Path(__file__).resolve(strict=True).parents[1]
        package = Path(harnessix.__file__).resolve(strict=True).parent
        if Path(config.source_root).resolve(strict=True) != root or package != (
            root / "src/harnessix"
        ).resolve(strict=True):
            raise ValueError
        result = subprocess.run(
            (
                str(Path(config.git_executable).resolve(strict=True)),
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            cwd=root,
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if result.returncode != 0 or result.stdout:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise KernelError(
            "verification_source_checkout_unavailable", "验证宿主源码来源或干净状态不可用"
        ) from None


def _require_scope(
    config: CodingEvalProviderSuiteRunConfig, observability: Observability | None
) -> None:
    # 保持原Pack、程序和HEAD拒绝优先级；只在受控付费入口增加准入检查。
    _require_runtime_scope(config, observability)
    _require_source_checkout(config)


def _bounds(config: CodingEvalProviderSuiteRunConfig) -> BailianVerificationBounds:
    """固定唯一认证范围；不从兼容协议名称推断地域、模式或价格。"""
    provider = config.provider_config
    prices = tuple(campaign.price for campaign in config.suite.campaign_plans)
    if (
        provider.base_url != "https://dashscope.aliyuncs.com/compatible-mode/v1"
        or provider.model != MODEL
        or provider.output_token_parameter != "max_tokens"
        or config.pack_id != "harnessix-engineering"
        or config.pack_version != 2
        or config.suite.fee_stop_currency != "CNY"
        or any(
            (
                price.billing_provider,
                price.model,
                price.region,
                price.service_tier,
                price.inference_mode,
                price.currency,
                price.source_url,
            )
            != ("aliyun-bailian", MODEL, "cn-beijing", "payg", "non-thinking", "CNY", PRICE_SOURCE)
            or not isinstance(price.input_price, FlatInputPrice)
            or price.input_price.per_million != "4"
            or price.output_per_million != "16"
            or price.input_tokens_min != 0
            or price.input_tokens_max != 32_000
            for price in prices
        )
    ):
        raise KernelError("verification_price_unavailable", "验证模型、地域或价格不在固定范围")
    bounds = BailianVerificationBounds(
        max(price.valid_from for price in prices),
        min(price.valid_until for price in prices),
        provider.max_output_tokens,
    )
    bounds.checkpoint()
    return bounds


def _require_images(config: CodingEvalProviderSuiteRunConfig) -> None:
    """只观察Pack固定镜像，不自动拉取、换Digest或改变Docker配置。"""
    loaded = builtin_coding_eval_task_pack(config.pack_id, config.pack_version)
    try:
        for image in sorted({profile.image for profile in loaded.manifest.profiles}):
            result = subprocess.run(
                (
                    config.container_engine,
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
            digests = json.loads(result.stdout) if result.returncode == 0 else None
            if not isinstance(digests, list) or image not in digests:
                raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise KernelError("verification_image_unavailable", "固定评测镜像尚未就绪") from None


def _credential(
    config: CodingEvalProviderSuiteRunConfig, service: str | None, account: str | None
) -> str:
    """凭据只进入短生命周期Adapter；不回显、不放入命令参数或全局环境。"""
    try:
        if bool(service) != bool(account):
            raise ValueError
        key = ""
        command: tuple[str, ...]
        if service is not None:
            if sys.platform != "darwin" or any(
                "\x00" in value for value in (service, account or "")
            ):
                raise ValueError
            command = (
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account or "",
                "-w",
            )
        elif sys.platform == "darwin":
            command = ("/bin/launchctl", "getenv", config.provider_config.api_key_env)
        else:
            command = ()
        if command:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
            if result.returncode == 0:
                key = result.stdout.decode("utf-8", errors="strict").rstrip("\n")
            elif service is not None:
                raise ValueError
        if not key and service is None:
            key = os.environ.get(config.provider_config.api_key_env, "")
        return validate_key(key, headers_env="OPENAI_CUSTOM_HEADERS")
    except (OSError, subprocess.SubprocessError, ValueError):
        raise KernelError(
            "verification_credentials_unavailable", "验证凭据未配置或不可用"
        ) from None


async def run_budgeted_suite(
    config: CodingEvalProviderSuiteRunConfig,
    *,
    budget_path: Path,
    period_id: UUID,
    allow_network: bool = False,
    resume: bool = False,
    keychain_service: str | None = None,
    keychain_account: str | None = None,
    reverification_id: UUID | None = None,
    minimum_request_interval_seconds: float = 0,
) -> CodingEvalSuiteRunReport:
    if allow_network is not True:
        raise KernelError("eval_provider_suite_network_disabled", "真实Provider Suite默认禁止网络")
    checked = CodingEvalProviderSuiteRunConfig.model_validate_json(
        config.model_dump_json(), strict=True
    )
    bounds = _bounds(checked)
    _require_scope(checked, None)
    _require_images(checked)
    pacer = VerificationRequestPacer(minimum_request_interval_seconds)
    cancellation = CancelToken()
    with VerificationBudgetLedger(
        budget_path,
        period_id,
        reverification_id=reverification_id,
        suite_id=checked.suite.plan.suite_id if reverification_id is not None else None,
    ) as ledger:
        key = _credential(checked, keychain_service, keychain_account)

        @asynccontextmanager
        async def factory(*_: object) -> AsyncIterator[ModelProvider]:
            async with OpenAIChatProvider(checked.provider_config, api_key=key) as provider:
                yield GuardedVerificationProvider(provider, ledger, bounds, cancellation, pacer)

        with provider_publication_scope(checked.provider_config.api_key_env, key) as scope:
            return await run_task_pack_provider_suite(
                checked,
                allow_network=True,
                resume=resume,
                cancel=cancellation,
                provider_factory=factory,
                provider_binding_sha256=bounds.fingerprint(ledger, pacer=pacer),
                publication_scope=scope,
            )


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.exit(2, "验证宿主参数无效；使用 --help 查看格式。\n")


def main(argv: Sequence[str] | None = None) -> None:
    parser = _SafeParser(
        description="持久预算保护的固定Task Pack验证宿主；默认不读取私有配置或凭据",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--budget-ledger", type=Path, required=True)
    parser.add_argument("--period-id", type=UUID, required=True)
    parser.add_argument("--reverification-id", type=UUID)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keychain-service")
    parser.add_argument("--keychain-account")
    parser.add_argument("--minimum-request-interval-seconds", type=float, default=0)
    arguments = parser.parse_args(argv)
    if not arguments.allow_network:
        print('{"reason":"network_not_enabled"}')
        raise SystemExit(2)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    result: dict[str, object] = {"reason": "verification_host_failed"}
    exit_code = 1
    try:
        config = read_private_eval_config(
            arguments.config, CodingEvalProviderSuiteRunConfig, max_bytes=2 * 1024 * 1024
        )
        report = asyncio.run(
            run_budgeted_suite(
                config,
                budget_path=arguments.budget_ledger,
                period_id=arguments.period_id,
                allow_network=True,
                resume=arguments.resume,
                keychain_service=arguments.keychain_service,
                keychain_account=arguments.keychain_account,
                reverification_id=arguments.reverification_id,
                minimum_request_interval_seconds=arguments.minimum_request_interval_seconds,
            )
        )
        result = report.model_dump(mode="json")
        exit_code = 0 if report.reason == "completed" else 1
    except KeyboardInterrupt:
        result = {"reason": "cancelled"}
        exit_code = 130
    except KernelError as error:
        # 只公开当前受控边界的有限错误码，不输出SDK异常、私有路径或第三方正文。
        if error.code in {
            "verification_price_unavailable",
            "verification_source_checkout_unavailable",
            "verification_image_unavailable",
            "verification_credentials_unavailable",
            "verification_request_pacing_invalid",
            "verification_budget_busy",
            "verification_budget_unavailable",
            "verification_budget_unresolved",
            "verification_budget_exhausted",
            "verification_budget_persist_failed",
            "eval_provider_suite_source_revision_mismatch",
            "eval_provider_suite_pack_mismatch",
            "eval_provider_suite_host_binding_invalid",
        }:
            result = {"reason": error.code}
    except Exception:
        pass
    finally:
        logging.disable(previous_logging)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
