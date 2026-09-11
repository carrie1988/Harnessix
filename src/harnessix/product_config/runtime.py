"""验证Profile选择、解析Environment Secret并装配Provider与审计约束的安全Fallback。"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal, Self, cast

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.domain.models import utc_now
from harnessix.models._provider_io import validate_key
from harnessix.models.config import AnthropicConfig, ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelProvider,
    ModelRequest,
    ProviderEvent,
    ResponseFailed,
)
from harnessix.product_config.contracts import (
    ConfigurationDiagnostic,
    ConfigurationDiagnosticReport,
    DiagnosticScope,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV2,
    ProfileSelection,
    ProviderDefinition,
    build_profile_selection,
    diagnostic_report_digest,
)
from harnessix.product_config.store import SQLiteProductConfigStore
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    SecretProvider,
)

_FALLBACK_CODES = frozenset({"transport", "rate_limit", "provider_internal"})
_SAFE_FALLBACK_METADATA_EVENTS = (
    ModelAttemptStarted,
    ModelUsageObserved,
    ModelAttemptFinished,
)


ProviderFactory = Callable[[ProviderDefinition, ModelProfile, str], ModelProvider]
DependencyFinder = Callable[[str], object | None]


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    profile: ModelProfile
    definition: ProviderDefinition
    provider: ModelProvider


def _verify_selection(snapshot: ProductConfigSnapshot, selection: ProfileSelection) -> None:
    if snapshot.config_sha256 != selection.config_sha256:
        raise KernelError("product_config_selection_mismatch", "Profile选择与配置快照不匹配")
    try:
        expected = build_profile_selection(
            snapshot.config,
            snapshot.config_sha256,
            selection.selected_profile,
        )
    except ValueError:
        raise KernelError(
            "product_config_selection_mismatch", "Profile选择不属于配置快照"
        ) from None
    if expected != selection:
        raise KernelError("product_config_selection_mismatch", "Profile选择内容与配置快照不一致")


def environment_secret_provider(
    config: ProductConfigV2,
    *,
    environment: Mapping[str, str] | None = None,
) -> EnvironmentSecretProvider:
    return EnvironmentSecretProvider(
        tuple(
            EnvironmentSecretSource(
                name=item.secret.name,
                version=item.secret.version,
                environment_variable=item.environment_variable,
            )
            for item in config.secret_sources
        ),
        environment=environment,
    )


def _provider_api_key(definition: ProviderDefinition, value: bytearray) -> str:
    try:
        key = bytes(value).decode("ascii", errors="strict")
        return validate_key(
            key,
            headers_env=(
                "OPENAI_CUSTOM_HEADERS"
                if definition.kind == "openai_chat"
                else "ANTHROPIC_CUSTOM_HEADERS"
            ),
        )
    except (UnicodeError, ValueError):
        raise KernelError("secret_unavailable", "Provider Secret格式无效") from None


def diagnose_configuration(
    snapshot: ProductConfigSnapshot,
    selection: ProfileSelection,
    secrets: SecretProvider,
    *,
    dependency_finder: DependencyFinder = importlib.util.find_spec,
) -> ConfigurationDiagnosticReport:
    _verify_selection(snapshot, selection)
    config = snapshot.config
    profiles = {item.profile_id: item for item in config.profiles}
    providers = {item.provider_id: item for item in config.providers}
    checks: dict[tuple[str, str, str], ConfigurationDiagnostic] = {}

    def record(scope: DiagnosticScope, subject: str, code: str, passed: bool) -> None:
        check = ConfigurationDiagnostic(
            scope=scope,
            subject_id=subject,
            code=code,
            status="passed" if passed else "failed",
        )
        checks[(check.scope, check.subject_id, check.code)] = check

    record("config", "product", "config_contract_valid", True)
    selected = profiles[selection.selected_profile]
    for profile_id in selection.profile_chain:
        profile = profiles[profile_id]
        provider = providers[profile.provider_id]
        record(
            "profile",
            profile.profile_id,
            "config_capabilities_satisfied",
            profile.capabilities.satisfies(selected.required_capabilities),
        )
        dependency = "openai" if provider.kind == "openai_chat" else "anthropic"
        try:
            present = dependency_finder(dependency) is not None
        except (ImportError, AttributeError, ValueError):
            present = False
        record("dependency", provider.provider_id, "config_dependency_available", present)
        material = None
        available = False
        usable = False
        try:
            material = secrets.resolve(provider.credential.name)
            available = (
                material.name == provider.credential.name
                and material.version == provider.credential.version
                and bool(material.value)
            )
            if available:
                _provider_api_key(provider, material.value)
                usable = True
        except (KernelError, ValueError, TypeError):
            pass
        finally:
            if material is not None:
                material.clear()
        record("secret", provider.credential.name, "config_secret_version_available", available)
        record("provider", provider.provider_id, "config_provider_auth_usable", usable)
    ordered = tuple(checks[key] for key in sorted(checks))
    candidate = ConfigurationDiagnosticReport.model_construct(
        _fields_set=None,
        config_sha256=snapshot.config_sha256,
        selection_sha256=selection.selection_sha256,
        selected_profile=selection.selected_profile,
        ready=all(item.status == "passed" for item in ordered),
        checks=ordered,
        generated_at=utc_now(),
        report_sha256="0" * 64,
    )
    return ConfigurationDiagnosticReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=diagnostic_report_digest(candidate),
    )


def default_provider_factory(
    definition: ProviderDefinition,
    profile: ModelProfile,
    api_key: str,
) -> ModelProvider:
    capabilities = ChatCapabilities(**profile.capabilities.model_dump())
    if definition.kind == "openai_chat":
        from harnessix.models.openai_chat import OpenAIChatProvider

        return OpenAIChatProvider(
            OpenAIChatConfig(
                base_url=definition.base_url,
                model=profile.model,
                api_key_env="HARNESSIX_MANAGED_PROVIDER_SECRET",
                capabilities=capabilities,
                max_output_tokens=profile.max_output_tokens,
                timeout_seconds=profile.timeout_seconds,
                io_timeout_seconds=profile.io_timeout_seconds,
                max_attempts=profile.max_attempts,
                retry_delay_seconds=profile.retry_delay_seconds,
                max_request_bytes=profile.max_request_bytes,
                max_response_bytes=profile.max_response_bytes,
                max_frame_bytes=profile.max_frame_bytes,
                max_chunks=profile.max_chunks,
                output_token_parameter=(
                    definition.output_token_parameter or "max_completion_tokens"
                ),
            ),
            api_key=api_key,
        )
    from harnessix.models.anthropic import AnthropicProvider

    return AnthropicProvider(
        AnthropicConfig(
            base_url=definition.base_url,
            model=profile.model,
            api_key_env="HARNESSIX_MANAGED_PROVIDER_SECRET",
            capabilities=capabilities,
            max_output_tokens=profile.max_output_tokens,
            timeout_seconds=profile.timeout_seconds,
            io_timeout_seconds=profile.io_timeout_seconds,
            max_attempts=profile.max_attempts,
            retry_delay_seconds=profile.retry_delay_seconds,
            max_request_bytes=profile.max_request_bytes,
            max_response_bytes=profile.max_response_bytes,
            max_frame_bytes=profile.max_frame_bytes,
            max_chunks=profile.max_chunks,
        ),
        api_key=api_key,
    )


def _resolve_provider(
    definition: ProviderDefinition,
    profile: ModelProfile,
    secrets: SecretProvider,
    factory: ProviderFactory,
) -> ModelProvider:
    material = secrets.resolve(definition.credential.name)
    try:
        if (
            material.name != definition.credential.name
            or material.version != definition.credential.version
        ):
            raise KernelError("secret_version_changed", "Provider Secret版本已经变化")
        key = _provider_api_key(definition, material.value)
        return factory(definition, profile, key)
    finally:
        material.clear()


class SafeFallbackProvider:
    """统一尝试序号；只有零响应暴露且审计成功后才切换候选。"""

    def __init__(
        self,
        *,
        config_sha256: str,
        candidates: tuple[ProviderCandidate, ...],
        audit: SQLiteProductConfigStore | None,
    ) -> None:
        if not candidates:
            raise KernelError("product_provider_unavailable", "Provider候选为空")
        self.config_sha256 = config_sha256
        self.candidates = candidates
        self.audit = audit
        self._closed = False

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        if self._closed:
            yield ResponseFailed(code="invalid_request")
            return
        global_index = 0
        exposed = False
        for position, candidate in enumerate(self.candidates):
            switched = False
            async with aclosing(candidate.provider.stream(request, cancel)) as stream:
                async for event in stream:
                    cancel.checkpoint()
                    if isinstance(event, ModelAttemptStarted):
                        global_index += 1
                        if global_index > 32:
                            yield ResponseFailed(code="invalid_request")
                            return
                        yield event.model_copy(
                            update={
                                "index": global_index,
                                "provider": candidate.definition.provider_id,
                            }
                        )
                        continue
                    if isinstance(event, ResponseFailed):
                        audit = self.audit
                        can_switch = (
                            not exposed
                            and event.retryable
                            and event.code in _FALLBACK_CODES
                            and position + 1 < len(self.candidates)
                            and audit is not None
                        )
                        if can_switch:
                            following = self.candidates[position + 1]
                            try:
                                assert audit is not None
                                audit.record_fallback(
                                    config_sha256=self.config_sha256,
                                    thread_id=request.thread_id,
                                    turn_id=request.turn_id,
                                    step=request.step,
                                    from_profile=candidate.profile.profile_id,
                                    from_provider=candidate.definition.provider_id,
                                    to_profile=following.profile.profile_id,
                                    to_provider=following.definition.provider_id,
                                    failure_code=cast(
                                        Literal["transport", "rate_limit", "provider_internal"],
                                        event.code,
                                    ),
                                )
                            except Exception:
                                yield event
                                return
                            switched = True
                            break
                        yield event
                        return
                    # 只把已冻结的尝试/用量账本事件视为零暴露元数据；未来新增事件默认
                    # 关闭Fallback窗口，避免新类型被错误当作不可见事件。
                    if not isinstance(event, _SAFE_FALLBACK_METADATA_EVENTS):
                        exposed = True
                    yield event
            if not switched:
                return

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        for candidate in reversed(self.candidates):
            close = getattr(candidate.provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException as error:
                    failure = failure or error
        if failure is not None:
            raise failure

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("product_provider_closed", "Provider Bundle已经关闭")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


async def build_provider_bundle(
    snapshot: ProductConfigSnapshot,
    selection: ProfileSelection,
    secrets: SecretProvider,
    *,
    audit: SQLiteProductConfigStore | None,
    factory: ProviderFactory = default_provider_factory,
) -> SafeFallbackProvider:
    _verify_selection(snapshot, selection)
    providers = {item.provider_id: item for item in snapshot.config.providers}
    profiles = {item.profile_id: item for item in snapshot.config.profiles}
    candidates: list[ProviderCandidate] = []
    try:
        for profile_id in selection.profile_chain:
            profile = profiles[profile_id]
            definition = providers[profile.provider_id]
            candidates.append(
                ProviderCandidate(
                    profile=profile,
                    definition=definition,
                    provider=_resolve_provider(definition, profile, secrets, factory),
                )
            )
    except BaseException:
        for candidate in reversed(candidates):
            close = getattr(candidate.provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException:
                    # 构造失败时保留原始错误，同时尽力关闭全部已构造候选。
                    pass
        raise
    return SafeFallbackProvider(
        config_sha256=snapshot.config_sha256,
        candidates=tuple(candidates),
        audit=audit,
    )


def select_profile(snapshot: ProductConfigSnapshot, profile_id: str | None) -> ProfileSelection:
    try:
        return build_profile_selection(snapshot.config, snapshot.config_sha256, profile_id)
    except ValueError:
        raise KernelError("product_profile_not_found", "Profile不存在或Fallback图无效") from None
