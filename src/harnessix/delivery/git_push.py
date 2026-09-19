"""Workspace与Git交付：规范化远端身份并执行受策略约束的Git Push。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import GitDeliveryRuntime, _GitRunner
from harnessix.delivery.git_contracts import (
    GitPushForceMode,
    GitPushIntent,
    GitPushReceipt,
    GitRepositoryBinding,
    git_push_intent_digest,
    validate_git_branch_ref,
    validate_git_object_id,
    validate_git_remote_name,
)
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor, utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ReconciliationConclusion,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)

_SCP_REMOTE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]{1,64})@)?(?P<host>[A-Za-z0-9.-]{1,253}):(?P<path>[^:]+)$"
)
_MAX_REMOTE_URL_BYTES = 4096
GIT_PUSH_TOOL = "git.push"
GIT_PUSH_EXECUTOR = "delivery.git-push"
GIT_PUSH_SOURCE = "harnessix.product"


def git_push_implementation_digest() -> str:
    root = Path(__file__).parent
    try:
        return canonical_digest(
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / "git.py", root / "git_contracts.py", root / "git_push.py")
            }
        )
    except OSError:
        raise KernelError("git_capability_unavailable", "Git Push实现不可证明") from None


def canonical_git_remote_url(value: str, *, allow_file: bool = False) -> str:
    """把受支持Git remote规范化后再摘要；拒绝凭据、查询和路径歧义。"""

    if (
        not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_REMOTE_URL_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise KernelError("git_remote_url_invalid", "Git remote URL无效")
    path_candidate = Path(value)
    if path_candidate.is_absolute():
        if not allow_file:
            raise KernelError("git_remote_protocol_denied", "本地Git remote协议未获授权")
        try:
            return path_candidate.resolve(strict=True).as_uri()
        except OSError:
            raise KernelError("git_remote_url_invalid", "本地Git remote不存在") from None
    scp = _SCP_REMOTE.fullmatch(value)
    if scp is not None and "://" not in value:
        host = _canonical_host(scp.group("host"))
        path = _canonical_remote_path("/" + scp.group("path").lstrip("/"))
        user = f"{scp.group('user')}@" if scp.group("user") else ""
        return f"ssh://{user}{host}{path}"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise KernelError("git_remote_url_invalid", "Git remote URL无法解析") from None
    if (
        parsed.query
        or parsed.fragment
        or parsed.password is not None
        or parsed.scheme
        not in {
            "file",
            "https",
            "ssh",
        }
    ):
        raise KernelError("git_remote_protocol_denied", "Git remote协议或凭据形式未获授权")
    if parsed.scheme == "file":
        if not allow_file or parsed.netloc not in {"", "localhost"}:
            raise KernelError("git_remote_protocol_denied", "本地Git remote未获授权")
        try:
            return Path(unquote(parsed.path)).resolve(strict=True).as_uri()
        except (OSError, UnicodeError):
            raise KernelError("git_remote_url_invalid", "本地Git remote不存在") from None
    if (
        parsed.hostname is None
        or (parsed.scheme == "https" and parsed.username is not None)
        or (
            parsed.scheme == "ssh"
            and parsed.username is not None
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", parsed.username) is None
        )
    ):
        raise KernelError("git_remote_url_invalid", "Git remote主机或用户信息无效")
    host = _canonical_host(parsed.hostname)
    if ":" in host:
        host = f"[{host}]"
    user = f"{parsed.username}@" if parsed.username is not None else ""
    port_text = f":{port}" if port is not None else ""
    path = _canonical_remote_path(parsed.path)
    return f"{parsed.scheme}://{user}{host}{port_text}{path}"


def git_remote_url_digest(value: str, *, allow_file: bool = False) -> str:
    return canonical_digest(canonical_git_remote_url(value, allow_file=allow_file))


def _canonical_host(value: str) -> str:
    try:
        selected = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise KernelError("git_remote_url_invalid", "Git remote主机无效") from None
    try:
        address = ipaddress.ip_address(selected)
    except ValueError:
        selected = selected.removesuffix(".")
        labels = selected.split(".")
        if (
            not selected
            or len(selected) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or re.fullmatch(r"[a-z0-9-]+", label) is None
                for label in labels
            )
        ):
            raise KernelError("git_remote_url_invalid", "Git remote主机无效") from None
        return selected
    if getattr(address, "scope_id", None) is not None:
        raise KernelError("git_remote_url_invalid", "Git remote主机无效")
    return address.compressed.lower()


def _canonical_remote_path(value: str) -> str:
    try:
        decoded = unquote(value)
    except UnicodeError:
        raise KernelError("git_remote_url_invalid", "Git remote路径无效") from None
    if (
        not decoded.startswith("/")
        or decoded in {"", "/"}
        or "\\" in decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        or any(part in {"", ".", ".."} for part in decoded.split("/")[1:])
    ):
        raise KernelError("git_remote_url_invalid", "Git remote路径无效")
    return quote(decoded, safe="/-._~")


class GitPushActionExecutor:
    """直接执行已冻结Trusted Route；远端响应不确定时只允许事实对账。"""

    def __init__(
        self,
        *,
        delivery: GitDeliveryRuntime,
        repository_root: str | Path,
        repository: GitRepositoryBinding,
        git_executable: str | Path,
        state_root: str | Path,
        allowed_protocols: tuple[str, ...] = ("https", "ssh"),
        allow_file_remote: bool = False,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._delivery = delivery
        self._root = Path(repository_root)
        self._repository = repository
        self._git = _GitRunner(git_executable, Path(state_root))
        self._allow_file = allow_file_remote
        protocols = tuple(sorted({*allowed_protocols, *(("file",) if allow_file_remote else ())}))
        if not protocols or any(value not in {"file", "https", "ssh"} for value in protocols):
            raise KernelError("git_protocol_invalid", "Git Push协议白名单无效")
        self._protocols = protocols
        self._fault = fault or (lambda _: None)

    def prepare_intent(
        self,
        *,
        remote_name: str,
        local_ref: str,
        remote_ref: str,
        expected_remote_oid: str | None,
        force_mode: GitPushForceMode,
        idempotency_key: str,
        push_id: UUID | None = None,
    ) -> GitPushIntent:
        """只读取本地仓库事实构造Intent；远端探测必须另行获得网络授权。"""

        try:
            validate_git_remote_name(remote_name)
            validate_git_branch_ref(local_ref)
            validate_git_branch_ref(remote_ref)
            if expected_remote_oid is not None:
                validate_git_object_id(expected_remote_oid)
            if force_mode not in {"fast_forward_only", "force_with_lease"}:
                raise ValueError
            if not idempotency_key.strip() or len(idempotency_key) > 256:
                raise ValueError
        except (AttributeError, ValueError):
            raise KernelError("git_push_intent_invalid", "Git Push Intent无效") from None
        self._verify_repository()
        local_oid = self._git.oid(self._root, ("rev-parse", "--verify", local_ref))
        completed = self._git.run(self._root, ("remote", "get-url", "--push", remote_name))
        remote_url = _decode_single_line(
            completed.stdout,
            encoding="utf-8",
            error_code="git_remote_url_invalid",
            error_message="Git remote URL响应无效",
        )
        candidate = GitPushIntent.model_construct(
            _fields_set=None,
            push_id=push_id or uuid4(),
            repository_binding_digest=self._repository.digest,
            remote_name=remote_name,
            remote_url_sha256=git_remote_url_digest(remote_url, allow_file=self._allow_file),
            local_ref=local_ref,
            local_oid=local_oid,
            remote_ref=remote_ref,
            expected_remote_oid=expected_remote_oid,
            force_mode=force_mode,
            idempotency_key=idempotency_key,
            digest="0" * 64,
        )
        try:
            return GitPushIntent(
                **candidate.model_dump(exclude={"digest"}),
                digest=git_push_intent_digest(candidate),
            )
        except (TypeError, ValidationError, ValueError):
            raise KernelError("git_push_intent_invalid", "Git Push Intent无效") from None

    async def execute(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        intent = _validate_route_intent(route, arguments)
        try:
            return await asyncio.to_thread(self._execute, route, intent)
        except UncertainEffectError:
            raise
        except KernelError as error:
            return _git_push_outcome(route, kind="failed", error_code=error.code)

    async def reconcile(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        try:
            intent = _validate_route_intent(route, arguments)
            return await asyncio.to_thread(self._reconcile, route, intent)
        except KernelError as error:
            return _git_push_outcome(route, kind="unknown", error_code=error.code)

    def _execute(
        self,
        route: ActionRoutePlan,
        intent: GitPushIntent,
    ) -> ActionExecutionOutcome:
        try:
            self._verify_local(intent)
            before = self._remote_oid(intent)
        except KernelError as error:
            return _git_push_outcome(route, kind="failed", error_code=error.code)
        if before != intent.expected_remote_oid:
            return _git_push_outcome(route, kind="failed", error_code="git_push_remote_changed")
        try:
            if intent.force_mode == "fast_forward_only" and before is not None:
                ancestor = self._git.run(
                    self._root,
                    ("merge-base", "--is-ancestor", before, intent.local_oid),
                    accepted=(0, 1),
                )
                if ancestor.returncode != 0:
                    return _git_push_outcome(
                        route,
                        kind="failed",
                        error_code="git_push_non_fast_forward",
                    )
        except KernelError as error:
            return _git_push_outcome(route, kind="failed", error_code=error.code)
        lease = intent.expected_remote_oid or ("0" * len(intent.local_oid))
        try:
            completed = self._git.run(
                self._root,
                (
                    "push",
                    "--porcelain",
                    "--no-verify",
                    f"--force-with-lease={intent.remote_ref}:{lease}",
                    intent.remote_name,
                    f"{intent.local_ref}:{intent.remote_ref}",
                ),
                accepted=(0, 1, 128),
                timeout=120.0,
                allowed_protocols=self._protocols,
            )
            self._fault("git_push.after_command")
        except UncertainEffectError:
            raise
        except Exception:
            raise UncertainEffectError("Push调用开始后未获得确定结果") from None
        try:
            after = self._remote_oid(intent)
        except KernelError:
            raise UncertainEffectError("Push命令已返回但远端事实读取失败") from None
        if after == intent.local_oid:
            receipt = self._receipt(intent, after)
            return _git_push_outcome(
                route,
                kind="succeeded",
                output=receipt.model_dump(mode="json"),
            )
        if completed.returncode != 0 and after == intent.expected_remote_oid:
            return _git_push_outcome(route, kind="failed", error_code="git_push_rejected")
        return _git_push_outcome(route, kind="unknown", error_code="git_push_uncertain")

    def _reconcile(
        self,
        route: ActionRoutePlan,
        intent: GitPushIntent,
    ) -> ActionExecutionOutcome:
        self._verify_binding(intent)
        current = self._remote_oid(intent)
        if current == intent.local_oid:
            receipt = self._receipt(intent, current)
            return _git_push_outcome(
                route,
                kind="succeeded",
                output=receipt.model_dump(mode="json"),
            )
        if current == intent.expected_remote_oid:
            return _git_push_outcome(route, kind="failed", error_code="git_push_not_applied")
        return _git_push_outcome(
            route,
            kind="manual_intervention",
            error_code="git_push_remote_diverged",
        )

    def _verify_local(self, intent: GitPushIntent) -> None:
        self._verify_binding(intent)
        local = self._git.oid(self._root, ("rev-parse", "--verify", intent.local_ref))
        if local != intent.local_oid:
            raise KernelError("git_push_local_changed", "本地Push ref已经变化")

    def _verify_binding(self, intent: GitPushIntent) -> None:
        self._verify_repository()
        if intent.repository_binding_digest != self._repository.digest:
            raise KernelError("git_repository_changed", "Git仓库绑定已经变化")
        if intent.remote_name != self._remote_name(intent):
            raise KernelError("git_remote_changed", "Git remote名称与计划不一致")

    def _verify_repository(self) -> None:
        current = self._delivery.bind_repository(self._root, self._repository.workspace_id)
        if current != self._repository:
            raise KernelError("git_repository_changed", "Git仓库绑定已经变化")

    def _remote_name(self, intent: GitPushIntent) -> str:
        completed = self._git.run(self._root, ("remote", "get-url", "--push", intent.remote_name))
        value = _decode_single_line(
            completed.stdout,
            encoding="utf-8",
            error_code="git_remote_url_invalid",
            error_message="Git remote URL响应无效",
        )
        if git_remote_url_digest(value, allow_file=self._allow_file) != intent.remote_url_sha256:
            raise KernelError("git_remote_changed", "Git remote URL已经变化")
        return intent.remote_name

    def _remote_oid(self, intent: GitPushIntent) -> str | None:
        completed = self._git.run(
            self._root,
            ("ls-remote", "--refs", intent.remote_name, intent.remote_ref),
            timeout=60.0,
            allowed_protocols=self._protocols,
        )
        if not completed.stdout:
            return None
        try:
            line = _decode_single_line(
                completed.stdout,
                encoding="ascii",
                error_code="git_remote_output_invalid",
                error_message="Git远端ref响应无效",
            )
            oid, reference = line.split("\t", 1)
        except ValueError:
            raise KernelError("git_remote_output_invalid", "Git远端ref响应无效") from None
        if reference != intent.remote_ref or len(oid) != len(intent.local_oid):
            raise KernelError("git_remote_output_invalid", "Git远端ref响应不唯一")
        try:
            int(oid, 16)
        except ValueError:
            raise KernelError("git_remote_output_invalid", "Git远端OID无效") from None
        return oid

    @staticmethod
    def _receipt(intent: GitPushIntent, remote_oid: str) -> GitPushReceipt:
        observed = utc_now()
        candidate = GitPushReceipt.model_construct(
            _fields_set=None,
            push_id=intent.push_id,
            remote_name=intent.remote_name,
            remote_ref=intent.remote_ref,
            remote_oid=remote_oid,
            remote_url_sha256=intent.remote_url_sha256,
            observed_at=observed,
            digest="0" * 64,
        )
        return GitPushReceipt(
            **candidate.model_dump(exclude={"digest"}),
            digest=canonical_digest(
                candidate.model_dump(mode="json", exclude={"digest"}, warnings="error")
            ),
        )


def _decode_single_line(
    value: bytes,
    *,
    encoding: str,
    error_code: str,
    error_message: str,
) -> str:
    try:
        decoded = value.decode(encoding, errors="strict")
    except UnicodeError:
        raise KernelError(error_code, error_message) from None
    if decoded.endswith("\r\n"):
        decoded = decoded[:-2]
    elif decoded.endswith("\n"):
        decoded = decoded[:-1]
    if not decoded or "\n" in decoded or "\r" in decoded:
        raise KernelError(error_code, error_message)
    return decoded


def git_push_descriptor() -> ToolDescriptor:
    """返回模型可见的固定Git Push Intent合同。"""

    version = "1." + canonical_digest(
        {
            "implementation": "git-push-action/v1",
            "implementation_digest": git_push_implementation_digest(),
            "input": GitPushIntent.model_json_schema(),
            "output": GitPushReceipt.model_json_schema(),
        }
    )
    return ToolDescriptor(
        name=GIT_PUSH_TOOL,
        version=version,
        description="按已批准远端OID lease更新单个Git branch ref；结果丢失时只对账",
        input_schema=GitPushIntent.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def git_push_binding() -> TrustedToolBinding:
    """把Git Push合同绑定到产品内Trusted Action执行器。"""

    descriptor = git_push_descriptor()
    return build_trusted_tool_binding(
        source="builtin",
        source_id=GIT_PUSH_SOURCE,
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="external_reconcile",
        executor_id=GIT_PUSH_EXECUTOR,
    )


def resolve_git_push(intent: GitPushIntent) -> ResolvedAction:
    """把远端仓库与单一ref更新冻结为规范资源。"""

    return ResolvedAction(
        resources=(
            canonical_action_resource(
                kind="external",
                access="write",
                identifier={"remote": intent.remote_url_sha256, "ref": intent.remote_ref},
            ),
            canonical_action_resource(
                kind="git_ref",
                access="update",
                identifier={"remote": intent.remote_name, "ref": intent.remote_ref},
                attributes={"expected": intent.expected_remote_oid},
            ),
        )
    )


def build_git_push_definition(executor: GitPushActionExecutor) -> TrustedActionDefinition:
    """构造不经过HTTP、Worker或旧Effect Journal的Git Push定义。"""

    root = _resolve_repository_root(executor._root)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        intent = _git_push_arguments(arguments)
        if context.cwd != "." or _resolve_repository_root(context.workspace_root) != root:
            raise KernelError("git_workspace_mismatch", "Git Push与规划Workspace不一致")
        return resolve_git_push(intent)

    return TrustedActionDefinition(
        binding=git_push_binding(),
        input_model=GitPushIntent,
        resolve=resolve,
        executor=executor,
    )


def _resolve_repository_root(value: str | Path) -> Path:
    try:
        return Path(value).resolve(strict=True)
    except OSError:
        raise KernelError("git_repository_changed", "Git仓库根目录不可用") from None


def _git_push_arguments(arguments: BaseModel) -> GitPushIntent:
    try:
        return GitPushIntent.model_validate_json(arguments.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        raise KernelError("git_push_input_invalid", "Git Push输入无效") from None


def _validate_route_intent(
    route: ActionRoutePlan,
    arguments: BaseModel,
) -> GitPushIntent:
    intent = _git_push_arguments(arguments)
    expected_resources = tuple(
        sorted(
            resolve_git_push(intent).resources,
            key=lambda item: (
                item.kind,
                item.access,
                item.identifier_sha256,
                item.attributes_sha256,
            ),
        )
    )
    if (
        route.binding != git_push_binding()
        or route.execution.plan_id != route.invocation.invocation_id
        or route.external_action_id is None
        or route.invocation.arguments != intent.model_dump(mode="json")
        or route.invocation.idempotency_key != intent.idempotency_key
        or route.resources != expected_resources
    ):
        raise KernelError("git_push_action_mismatch", "Git Push与Action Route不一致")
    return intent


def _git_push_outcome(
    route: ActionRoutePlan,
    *,
    kind: ReconciliationConclusion,
    output: JsonValue | None = None,
    error_code: str | None = None,
) -> ActionExecutionOutcome:
    return ActionExecutionOutcome(
        kind=kind,
        output=output,
        external_action_id=route.external_action_id,
        error_code=error_code,
    )
