"""产品配置：把非敏感草案构造成v2配置并以CAS原子提交。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast
from urllib.parse import urlsplit
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.models.config import LOOPBACK_HOSTS
from harnessix.product_config.codec import (
    canonical_product_config_bytes,
    load_product_config,
    read_product_config_bytes,
)
from harnessix.product_config.contracts import (
    CONTEXT_OVERHEAD_TOKENS,
    THINKING_MIN_OUTPUT_TOKENS,
    EnvironmentSecretSourceConfig,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
    ThinkingProtocol,
    product_config_digest,
)
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    configuration_write_receipt_digest,
)
from harnessix.product_config.reasoning_models import reasons_by_default
from harnessix.product_config.vendors import endpoint_host


@dataclass(frozen=True, slots=True)
class ConfigurationWriteRequest:
    """一次明确的新建或替换请求；替换必须携带旧文件摘要。"""

    path: Path
    draft: ConfigurationDraft
    expected_source_sha256: str | None = None
    replace: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedWrite:
    config: ProductConfigV2
    body: bytes
    source_sha256: str
    config_sha256: str
    parent: Path
    target: Path
    temporary: Path


@dataclass(slots=True)
class _CommitState:
    committed: bool = False
    previous_source_sha256: str | None = None


# 思考Profile的传输上限沿用SWE-bench v40实测值：推理与回答共用32768输出，约一个SSE帧/Token、
# 约60字节/Token；单步生成32768 Token约需十余分钟，请求与流间隔超时随之放宽。
_THINKING_LIMITS: dict[str, object] = {
    "max_output_tokens": 32_768,
    "max_chunks": 80_000,
    "max_response_bytes": 12 * 1024 * 1024,
    "timeout_seconds": 900.0,
    "io_timeout_seconds": 180.0,
}

# 同机自托管端点：本地加速卡prefill慢且多会话共享副本，首字节在prefill完成后才到达，
# 默认30秒首包/120秒单请求时限会把排队中的正常请求判为传输失败。
_DEFAULT_MAX_OUTPUT_TOKENS = 4096
_SELF_HOSTED_READ_TIMEOUT_SECONDS = 20
_SELF_HOSTED_LIMITS: dict[str, object] = {
    "timeout_seconds": 1800.0,
    "io_timeout_seconds": 300.0,
}


# 官方文档核对（智谱2026-11-10，其余2026-11-09）的按量调用上下文长度；未列出的不预设。
_KNOWN_CONTEXT_WINDOWS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("api.deepseek.com", ("deepseek-v4", "deepseek-flash"), 1_000_000),
    ("dashscope.aliyuncs.com", ("deepseek-v4", "qwen3.8-max", "qwen3.8-flash"), 1_000_000),
    ("open.bigmodel.cn", ("glm-5.3",), 1_000_000),
)
# 智谱文档：tool_choice“默认且仅支持auto”，请求只认max_tokens。
_AUTO_TOOL_CHOICE_ONLY_HOSTS = frozenset({"open.bigmodel.cn"})
_MAX_TOKENS_HOSTS = frozenset({"open.bigmodel.cn"})


def known_context_window(base_url: str, model: str) -> int | None:
    """已核对端点与模型族的上下文窗口；其余返回None，沿用产品固定压缩上限。"""

    host = endpoint_host(base_url)
    for known_host, prefixes, window in _KNOWN_CONTEXT_WINDOWS:
        if host == known_host and model.startswith(prefixes):
            return window
    return None


def _allowed_output_tokens(window: int) -> int:
    return (window - 1) // 2 - CONTEXT_OVERHEAD_TOKENS


def _default_output_cap(window: int) -> int:
    """默认输出上限不超过窗口的四分之一：输出预留越大，留给历史的空间越小。

    窗口允许的最大值约是窗口的45%。32K窗口用它时，扣除工具定义与指令后历史只剩约4万字节，
    任务读几个文件就要压缩；真实运行里单次输出最多用到约4千Token。
    """
    return min(_allowed_output_tokens(window), window // 4)


def _window_fits_thinking(window: int | None) -> bool:
    return window is None or _default_output_cap(window) >= THINKING_MIN_OUTPUT_TOKENS


def _output_limits(
    window: int | None, *, thinking: bool, thinking_requested: bool = False
) -> dict[str, object]:
    """声明了窗口时按窗口收紧默认输出上限，避免生成无法启动或把窗口大半留给输出的配置。

    用户显式要求思考（thinking_requested）时放到窗口允许的最大值：推理计入输出上限。
    """

    limits: dict[str, object] = dict(_THINKING_LIMITS) if thinking else {}
    if window is None:
        return limits
    cap = _allowed_output_tokens(window) if thinking_requested else _default_output_cap(window)
    current = cast(int, limits.get("max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS))
    if current > cap:
        limits["max_output_tokens"] = cap
    return limits


def thinking_preset(
    base_url: str, model: str, *, self_hosted: bool = False
) -> tuple[ThinkingProtocol, bool] | None:
    """已求证端点与模型族的思考预设：（思考协议，是否不发送tool_choice）；其余端点不预设。"""

    host = endpoint_host(base_url)
    if host in LOOPBACK_HOSTS or self_hosted:
        # 自托管vLLM：思考开关经chat_template_kwargs交给模型对话模板。
        return "vllm_chat_template", False
    if host == "dashscope.aliyuncs.com" and model.startswith("deepseek-v4"):
        # 百炼DeepSeek的required/指定函数tool_choice经约束解码语法编译，间歇超时并返回400。
        return "dashscope_enable_thinking", True
    if host == "dashscope.aliyuncs.com" and model.startswith(("qwen3.8-max", "qwen3.8-flash")):
        # 百炼文档（2026-11-11核对）：这两个型号是混合思考、默认开启，可用enable_thinking关闭；
        # 同系列另有只能思考的型号，因此只列已核对的型号前缀。不预设时每个请求都按服务端默认思考，
        # 摘要这类输出额度小的无工具请求会被推理占满而没有正文。
        return "dashscope_enable_thinking", False
    if host == "api.deepseek.com" and model.startswith(("deepseek-v4", "deepseek-flash")):
        return "deepseek_thinking_type", False
    return None


def effort_tiers_verified(base_url: str, model: str) -> bool:
    """这个端点与模型是否核实过思考强度档位（以及思考Token上限）。

    百炼qwen3.8-max、qwen3.8-flash（2026-11-12实测）：不设档位时按服务端默认的最强档思考，
    同一个简单任务思考17,378 Token、耗时5分钟；设为low或medium后降到一两百Token。
    同协议的百炼DeepSeek没有核实，不在此列。
    """
    host = endpoint_host(base_url)
    return host == "dashscope.aliyuncs.com" and model.startswith(("qwen3.8-max", "qwen3.8-flash"))


def _thinking_bounds(draft: ConfigurationDraft, *, thinking: bool) -> dict[str, object]:
    """Profile的思考强度或思考Token上限：核实过档位的模型默认中档；给了上限就只用上限。"""
    if not thinking or draft.thinking_budget_tokens is not None:
        return {"thinking_budget_tokens": draft.thinking_budget_tokens if thinking else None}
    verified = effort_tiers_verified(draft.base_url, draft.model)
    return {"thinking_effort": draft.thinking_effort or ("medium" if verified else None)}


def auto_tool_choice_only(base_url: str) -> bool:
    """端点只接受tool_choice=auto：不发送tool_choice，强制选择改由可执行集收窄约束。"""

    return urlsplit(base_url).hostname in _AUTO_TOOL_CHOICE_ONLY_HOSTS


def build_product_config(draft: ConfigurationDraft) -> ProductConfigV2:
    """使用现有安全默认值生成单Provider、单Profile配置；声明思考协议时默认只在工具步骤思考。"""

    checked = ConfigurationDraft.model_validate_json(draft.model_dump_json(), strict=True)
    secret = SecretReference(name=checked.secret_name, version=checked.secret_version)
    protocol = checked.thinking_protocol
    thinking = protocol is not None and checked.thinking != "off"
    # 小窗口放不下思考所需的输出上限：未显式要求思考时自动关闭，而不是生成无法启动的配置。
    thinking_auto_off = (
        thinking
        and checked.thinking is None
        and not _window_fits_thinking(checked.context_window_tokens)
    )
    thinking = thinking and not thinking_auto_off
    output_token_parameter = checked.output_token_parameter or (
        "max_tokens"
        if protocol is not None or urlsplit(checked.base_url).hostname in _MAX_TOKENS_HOSTS
        else None
    )
    # 自托管端点（本机回环或已声明的内网）：畸形工具参数只拒绝该次调用，并按0元计价。
    self_hosted = checked.self_hosted or urlsplit(checked.base_url).hostname in LOOPBACK_HOSTS
    return ProductConfigV2(
        active_profile=checked.profile_id,
        # 同机自托管通常与推理、构建共用CPU：读工具放宽到20秒，避免大仓库搜索在负载下超时。
        workspace_read_timeout_seconds=_SELF_HOSTED_READ_TIMEOUT_SECONDS if self_hosted else None,
        service_failure_retries=checked.service_failure_retries,
        secret_sources=(
            EnvironmentSecretSourceConfig(
                secret=secret,
                environment_variable=checked.environment_variable,
            ),
        ),
        providers=(
            ProviderDefinition(
                provider_id=checked.provider_id,
                kind=checked.provider_kind,
                base_url=checked.base_url,
                credential=secret,
                output_token_parameter=output_token_parameter,
                thinking_protocol=protocol,
                omit_tool_choice=checked.omit_tool_choice,
                # 云端与自托管的模型都会偶尔把数组、对象参数写成JSON字符串；还原只在Schema声明
                # 该位置为容器、且字符串能严格解析成同类容器时发生，对写对了的调用没有影响。
                normalize_tool_arguments=True,
                repair_tool_arguments=self_hosted,
                stable_prompt_prefix=self_hosted,
                compact_tool_results=checked.provider_kind == "openai_chat",
                pricing="free" if self_hosted else "unknown",
                self_hosted=checked.self_hosted,
                proxy_url=checked.proxy_url,
            ),
        ),
        profiles=(
            ModelProfile.model_validate(
                {
                    "profile_id": checked.profile_id,
                    "provider_id": checked.provider_id,
                    "model": checked.model,
                    "thinking": "off" if thinking_auto_off else checked.thinking,
                    **_thinking_bounds(checked, thinking=thinking),
                    "context_window_tokens": checked.context_window_tokens,
                    # 云端限流和网络抖动很常见：首次请求之外最多自动重试5次（ADR 0144）。
                    "max_attempts": 6,
                    "retry_delay_seconds": 1.0,
                    **_output_limits(
                        checked.context_window_tokens,
                        thinking=thinking or reasons_by_default(checked.base_url, checked.model),
                        thinking_requested=checked.thinking not in (None, "off"),
                    ),
                    **(_SELF_HOSTED_LIMITS if self_hosted else {}),
                }
            ),
        ),
    )


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _prepare_private_parent(path: Path) -> Path:
    parent = path.parent
    try:
        if parent.exists() or _is_link_or_junction(parent):
            info = parent.lstat()
        else:
            parent.mkdir(parents=True, mode=0o700)
            info = parent.lstat()
        if _is_link_or_junction(parent) or not stat.S_ISDIR(info.st_mode):
            raise OSError
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError
        return parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError(
            "product_config_permissions",
            "产品配置目录权限或身份不安全",
        ) from None


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        except FileExistsError:
            descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
            or opened.st_size < 1
            or (
                os.name == "posix"
                and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600)
            )
        ):
            raise OSError
        os.lseek(descriptor, 0, os.SEEK_SET)
        acquire_exclusive_file_lock(descriptor)
        return descriptor
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError(
            "product_config_lock_timeout",
            "产品配置写入锁被占用",
            retryable=True,
        ) from None
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError("product_config_lock", "产品配置写入锁不安全") from None


def _write_private_file(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_expected_digest(value: str | None) -> None:
    if value is None:
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise KernelError("product_config_write_invalid", "产品配置期望摘要无效")


def current_config_digest(target: Path) -> str | None:
    """现有配置文件的摘要，替换时作为期望值；读不到时返回None。"""

    try:
        return hashlib.sha256(read_product_config_bytes(target)).hexdigest()
    except (OSError, KernelError):
        return None


def _replace_hint(target: Path) -> str:
    """给出当前文件摘要：用户照此替换，摘要仍挡住期间被别人改写的配置。"""
    digest = current_config_digest(target)
    if digest is None:
        return ""
    return f"；要替换它，加 --replace --expected-source-sha256 {digest}"


def _validate_write_request(request: ConfigurationWriteRequest) -> None:
    _validate_expected_digest(request.expected_source_sha256)
    if request.replace and request.expected_source_sha256 is None:
        raise KernelError(
            "product_config_expected_digest_required",
            "替换产品配置必须提供源摘要" + _replace_hint(request.path),
        )
    if not request.replace and request.expected_source_sha256 is not None:
        raise KernelError("product_config_write_invalid", "新建产品配置不能提供源摘要")
    if not request.path.name or request.path.name in {".", ".."}:
        raise KernelError("product_config_write_invalid", "产品配置写入路径无效")


def _prepare_write(request: ConfigurationWriteRequest) -> _PreparedWrite:
    _validate_write_request(request)
    config = ProductConfigV2.model_validate_json(
        build_product_config(request.draft).model_dump_json(),
        strict=True,
    )
    body = canonical_product_config_bytes(config)
    parent = _prepare_private_parent(request.path.absolute())
    target = parent / request.path.name
    return _PreparedWrite(
        config=config,
        body=body,
        source_sha256=hashlib.sha256(body).hexdigest(),
        config_sha256=product_config_digest(config),
        parent=parent,
        target=target,
        temporary=parent / f".{target.name}.{uuid4().hex}.tmp",
    )


def _validate_target(
    request: ConfigurationWriteRequest,
    prepared: _PreparedWrite,
    state: _CommitState,
) -> None:
    exists = prepared.target.exists() or _is_link_or_junction(prepared.target)
    if not request.replace and exists:
        raise KernelError(
            "product_config_exists", "产品配置已存在" + _replace_hint(prepared.target)
        )
    if request.replace and not exists:
        raise KernelError("product_config_conflict", "待替换产品配置不存在")
    if request.replace:
        state.previous_source_sha256 = hashlib.sha256(
            read_product_config_bytes(prepared.target)
        ).hexdigest()
        if state.previous_source_sha256 != request.expected_source_sha256:
            raise KernelError("product_config_conflict", "产品配置源摘要已变化")


def _commit_configuration(
    request: ConfigurationWriteRequest,
    prepared: _PreparedWrite,
    state: _CommitState,
    trigger: Callable[[str], None],
) -> None:
    _validate_target(request, prepared, state)
    _write_private_file(prepared.temporary, prepared.body)
    trigger("configuration.after_temporary_fsync")
    if request.replace:
        current = hashlib.sha256(read_product_config_bytes(prepared.target)).hexdigest()
        if current != request.expected_source_sha256:
            raise KernelError("product_config_conflict", "产品配置在写入期间发生变化")
        os.replace(prepared.temporary, prepared.target)
    else:
        os.link(prepared.temporary, prepared.target)
        prepared.temporary.unlink()
    state.committed = True
    _sync_directory(prepared.parent)
    trigger("configuration.after_replace_fsync")


def _verify_commit(prepared: _PreparedWrite) -> None:
    reopened = load_product_config(prepared.target)
    if not isinstance(reopened, ProductConfigSnapshot) or (
        reopened.source_sha256 != prepared.source_sha256
        or reopened.config_sha256 != prepared.config_sha256
        or reopened.config != prepared.config
    ):
        raise KernelError("product_config_commit_unknown", "产品配置提交结果无法确认")


def _raise_write_failure(committed: bool, *, target_exists: bool = False) -> Never:
    if committed:
        raise KernelError("product_config_commit_unknown", "产品配置提交结果无法确认") from None
    if target_exists:
        raise KernelError("product_config_exists", "产品配置已存在") from None
    raise KernelError("product_config_write_failed", "产品配置写入未完成") from None


def _receipt(
    *,
    operation: Literal["created", "replaced"],
    previous_source_sha256: str | None,
    source_sha256: str,
    config_sha256: str,
) -> ConfigurationWriteReceipt:
    candidate = ConfigurationWriteReceipt.model_construct(
        operation=operation,
        previous_source_sha256=previous_source_sha256,
        source_sha256=source_sha256,
        config_sha256=config_sha256,
        occurred_at=utc_now(),
        receipt_sha256="0" * 64,
    )
    return ConfigurationWriteReceipt(
        **candidate.model_dump(exclude={"receipt_sha256"}),
        receipt_sha256=configuration_write_receipt_digest(candidate),
    )


def write_product_config(
    request: ConfigurationWriteRequest,
    *,
    fault: Callable[[str], None] | None = None,
) -> ConfigurationWriteReceipt:
    """在私有目录中创建配置，或按源字节摘要CAS替换现有配置。"""

    prepared = _prepare_write(request)
    lock_descriptor = _open_lock(prepared.parent / f".{prepared.target.name}.configure.lock")
    trigger = fault or (lambda _: None)
    state = _CommitState()
    try:
        _commit_configuration(request, prepared, state, trigger)
        _verify_commit(prepared)
        return _receipt(
            operation="replaced" if request.replace else "created",
            previous_source_sha256=state.previous_source_sha256,
            source_sha256=prepared.source_sha256,
            config_sha256=prepared.config_sha256,
        )
    except KernelError:
        if state.committed:
            _raise_write_failure(True)
        raise
    except FileExistsError:
        _raise_write_failure(state.committed, target_exists=True)
    except OSError:
        _raise_write_failure(state.committed)
    except Exception:
        _raise_write_failure(state.committed)
    finally:
        try:
            prepared.temporary.unlink()
        except OSError:
            pass
        try:
            os.close(lock_descriptor)
        except OSError:
            pass
