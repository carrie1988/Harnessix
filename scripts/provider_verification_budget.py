"""真实Provider验证宿主的持久预留账本；不创建预算、不保存凭据或正文。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.models._json import strict_json
from harnessix.models.pricing import amount_units, format_amount
from harnessix.product_config import session_key_posix as private_fs
from scripts.provider_reverification_binding import (
    VerificationReverificationBinding,
    validate_reverification_binding,
)
from scripts.provider_reverification_chain import (
    CHAIN_SCHEMA,
    MAX_CANDIDATE_BINDINGS,
    VerificationCandidateBinding,
    candidate_bindings,
    snapshot_candidate_binding,
    validate_candidate_chain,
)
from scripts.provider_reverification_plan import (
    VerificationBetaTaskReverificationPlan,
    VerificationReverificationPlanRecord,
    parse_reverification_plan,
    snapshot_reverification_plan,
    validate_reverification_plan,
)
from scripts.provider_task_continuation import (
    TASK_CONTINUATION_CHAIN_SCHEMA,
    TASK_CONTINUATION_SCHEMA,
    TaskContinuationRecord,
    VerificationTaskContinuationV2,
    parse_task_continuation,
    snapshot_task_continuation,
    task_continuation_chain,
    validate_task_continuation,
)
from scripts.provider_usage_reconciliation import (
    BUDGET_SCHEMA as USAGE_RECONCILIATION_SCHEMA,
)
from scripts.provider_usage_reconciliation import (
    UsageReconciliation,
    reconciliation_costs,
    snapshot_usage_reconciliation,
    usage_reconciliations,
)

_MAX_LEDGER_BYTES = 1024 * 1024
_SCHEMA = "harnessix.provider-verification-budget/v1"
_REBOUND_SCHEMA = "harnessix.provider-verification-budget/v2"


def _amount(value: object) -> int:
    """沿用18位货币定点整数；JSON数字、指数和负金额不能强转进入账本。"""
    if not isinstance(value, str):
        raise ValueError
    whole, _, fraction = value.partition(".")
    if not whole.isascii() or not whole.isdecimal() or len(fraction) > 18:
        raise ValueError
    if "." in value and (not fraction.isascii() or not fraction.isdecimal()):
        raise ValueError
    return amount_units(value)


class VerificationBudgetLedger:
    """独占原账本的同步Owner；每次请求先持久预留，未知金额不退款。"""

    def __init__(
        self,
        path: Path,
        period_id: UUID,
        *,
        reverification_id: UUID | None = None,
        suite_id: UUID | None = None,
        task_id: str | None = None,
        task_continuation_id: UUID | None = None,
    ) -> None:
        self.path = path.absolute()
        self.period_id = str(period_id)
        self.root: int | None = None
        self.lock: int | None = None
        self._body = b""
        self._root_identity: tuple[int, ...] = ()
        self.data: dict[str, Any] = {}
        self.allocation = 0
        self.reverification_id = reverification_id
        self.suite_id = suite_id
        self.task_id = task_id
        self.task_continuation_id = task_continuation_id
        self._registration_only = False
        self._candidate_registration = False
        self._continuation_registration = False
        self._usage_reconciliation_registration = False

    def __enter__(self) -> Self:
        if self.root is not None:
            raise KernelError("verification_budget_busy", "验证预算Owner不能重复进入")
        if os.name != "posix":
            raise KernelError("verification_budget_host_unsupported", "验证账本需要POSIX私有宿主")
        try:
            self.root = os.open(
                self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            info = os.fstat(self.root)
            private_fs._private(info, directory=True)
            private_fs._private_acl(self.root)
            self._root_identity = private_fs._directory_identity(info)
            self.lock = os.open(
                ".provider-verification-budget.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.root,
            )
            private_fs._private(os.fstat(self.lock))
            private_fs._private_acl(self.lock)
            acquire_exclusive_file_lock(self.lock)
            self._body = self._read()
            self.data = self._validate(strict_json(self._body))
            self.allocation = _amount(self.period["allocation"])
            if not self._registration_only:
                self.require_available()
            return self
        except BlockingIOError:
            self.close()
            raise KernelError("verification_budget_busy", "验证预算已有活跃宿主") from None
        except KernelError as error:
            self.close()
            if error.code == "verification_budget_unresolved":
                raise
            raise KernelError("verification_budget_unavailable", "验证预算不可用") from None
        except Exception:
            self.close()
            raise KernelError(
                "verification_budget_unavailable", "验证预算不可用或有未决请求"
            ) from None

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """关闭Owner，不删除持久预留或初始化锁。"""
        for name in ("lock", "root"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    @property
    def period(self) -> dict[str, Any]:
        return next(p for p in self.data["periods"] if p["period_id"] == self.period_id)

    @property
    def reverification_plan(self) -> VerificationReverificationPlanRecord | None:
        """读取原周期唯一授权；未登记账本保持默认未决即停语义。"""
        raw = self.period.get("bounded_reverification")
        if raw is None:
            return None
        return parse_reverification_plan(json.dumps(raw))

    @property
    def task_continuation(self) -> TaskContinuationRecord | None:
        chain = task_continuation_chain(self.period)
        if chain:
            return chain[-1]
        raw = self.period.get("task_continuation")
        return None if raw is None else parse_task_continuation(json.dumps(raw))

    @classmethod
    def authorize_task_continuation(cls, path: Path, record: TaskContinuationRecord) -> None:
        """原V1不替换；另授V2只追加，相等重试不重置请求次数或金额。"""
        try:
            checked = snapshot_task_continuation(record)
        except Exception:
            raise KernelError("verification_reverification_invalid", "任务承接合同无效") from None
        owner = cls(path, checked.period_id)
        owner._registration_only = owner._continuation_registration = True
        with owner:
            existing = owner.task_continuation
            if existing == checked:
                assert owner.root is not None
                try:
                    os.fsync(owner.root)
                except OSError:
                    raise KernelError(
                        "verification_budget_persist_failed", "任务承接未能可靠确认"
                    ) from None
                return
            successor = type(checked) is VerificationTaskContinuationV2
            if (existing is not None and not successor) or (
                successor
                and (
                    existing is None or checked.previous_continuation_id != existing.continuation_id
                )
            ):
                raise KernelError("verification_reverification_invalid", "不能替换或跳过任务承接")
            plan = owner.reverification_plan
            if (
                plan is None
                or checked.ledger_before_sha256 != sha256(owner._body).hexdigest()
                or checked.prior_request_count != len(owner.period["requests"])
            ):
                raise KernelError("verification_reverification_invalid", "承接不属于原账本")
            if successor:
                owner.period.setdefault("task_continuation_chain", []).append(
                    checked.model_dump(mode="json")
                )
                owner.data["schema"] = TASK_CONTINUATION_CHAIN_SCHEMA
            else:
                owner.period["task_continuation"] = checked.model_dump(mode="json")
                owner.data["schema"] = TASK_CONTINUATION_SCHEMA
            try:
                validate_task_continuation(owner.period, plan, checked)
            except ValueError:
                raise KernelError(
                    "verification_reverification_invalid", "任务承接前缀无效"
                ) from None
            # 不理解单请求撤销语义的旧Reader必须拒绝，不得恢复原task权限。
            owner._save()

    @property
    def reverification_binding(self) -> VerificationReverificationBinding | None:
        """读取单次Suite切换；原复验授权及所有历史请求仍保持原件。"""
        raw = self.period.get("reverification_binding")
        if raw is None:
            return None
        return VerificationReverificationBinding.model_validate_json(json.dumps(raw), strict=True)

    @property
    def active_reverification_binding(
        self,
    ) -> VerificationReverificationBinding | VerificationCandidateBinding | None:
        """新链只授权末尾候选；旧单次绑定仍为不可变历史，不恢复其请求权限。"""
        bindings = candidate_bindings(self.period)
        return bindings[-1] if bindings else self.reverification_binding

    @classmethod
    def append_reverification_binding(
        cls, path: Path, binding: VerificationCandidateBinding
    ) -> None:
        """显式追加同一原额度内的候选；不联网、不激活旧Suite、不新增预算。"""
        try:
            checked = snapshot_candidate_binding(binding)
        except Exception:
            raise KernelError("verification_reverification_invalid", "追加候选记录无效") from None
        owner = cls(path, checked.period_id)
        owner._registration_only = owner._candidate_registration = True
        with owner:
            existing = candidate_bindings(owner.period)
            for item in existing:
                if item.binding_id == checked.binding_id:
                    if item != checked:
                        raise KernelError("verification_reverification_invalid", "候选身份已存在")
                    assert owner.root is not None
                    try:
                        os.fsync(owner.root)
                    except OSError:
                        raise KernelError(
                            "verification_budget_persist_failed", "候选追加未能可靠确认"
                        ) from None
                    return
            plan, original = owner.reverification_plan, owner.reverification_binding
            if (
                plan is None
                or isinstance(plan, VerificationBetaTaskReverificationPlan)
                or original is None
                or len(existing) >= MAX_CANDIDATE_BINDINGS
                or checked.ledger_before_sha256 != sha256(owner._body).hexdigest()
                or checked.prior_request_count != len(owner.period["requests"])
            ):
                raise KernelError("verification_reverification_invalid", "候选不属于当前原账本")
            try:
                validate_candidate_chain(owner.period, plan, original, (*existing, checked))
            except ValueError:
                raise KernelError(
                    "verification_reverification_invalid", "候选前驱或原累计费用不一致"
                ) from None
            owner.period["reverification_binding_chain"] = [
                item.model_dump(mode="json") for item in (*existing, checked)
            ]
            owner.data["schema"] = CHAIN_SCHEMA
            owner._save()

    @classmethod
    def reconcile_usage(cls, path: Path, record: UsageReconciliation) -> None:
        """追加完整 usage 对账；原 unknown 请求和旧账本哈希保持可审计。"""
        try:
            checked = snapshot_usage_reconciliation(record)
        except Exception:
            raise KernelError(
                "verification_usage_reconciliation_invalid", "usage 对账合同无效"
            ) from None
        owner = cls(path, checked.period_id)
        owner._registration_only = owner._usage_reconciliation_registration = True
        try:
            with owner:
                existing = usage_reconciliations(owner.period)
                for item in existing:
                    if item.reconciliation_id == checked.reconciliation_id:
                        if item != checked:
                            raise KernelError(
                                "verification_usage_reconciliation_invalid", "对账身份已存在"
                            )
                        assert owner.root is not None
                        os.fsync(owner.root)
                        return
                    if item.request_id == checked.request_id:
                        raise KernelError(
                            "verification_usage_reconciliation_invalid", "请求已经完成 usage 对账"
                        )
                if checked.ledger_before_sha256 != sha256(
                    owner._body
                ).hexdigest() or checked.previous_reconciliation_id != (
                    existing[-1].reconciliation_id if existing else None
                ):
                    raise KernelError(
                        "verification_usage_reconciliation_invalid", "对账不属于当前原账本"
                    )
                request = next(
                    item
                    for item in owner.period["requests"]
                    if item["request_id"] == str(checked.request_id)
                )
                held = _amount(request["reserved_cost"])
                cost = _amount(checked.cost_estimate)
                owner.period.setdefault("usage_reconciliations", []).append(
                    checked.model_dump(mode="json")
                )
                owner.period["known_cost"] = format_amount(
                    _amount(owner.period["known_cost"]) + cost
                )
                owner.period["reserved_cost"] = format_amount(
                    _amount(owner.period["reserved_cost"]) - held
                )
                owner.data["schema"] = USAGE_RECONCILIATION_SCHEMA
                owner._save()
        except KernelError as error:
            if error.code == "verification_usage_reconciliation_invalid":
                raise
            raise KernelError(
                "verification_usage_reconciliation_invalid", "usage 对账未通过"
            ) from None
        except Exception:
            raise KernelError(
                "verification_usage_reconciliation_invalid", "usage 对账未通过"
            ) from None

    @classmethod
    def authorize_reverification(
        cls, path: Path, plan: VerificationReverificationPlanRecord
    ) -> None:
        """仅可信预算管理宿主调用；独占登记后退出，不读取凭据或发出模型请求。"""
        try:
            plan = snapshot_reverification_plan(plan)
        except Exception:
            raise KernelError("verification_reverification_invalid", "复验授权合同无效") from None
        owner = cls(path, plan.period_id)
        owner._registration_only = True
        with owner:
            existing = owner.reverification_plan
            if existing is not None:
                if existing != plan:
                    raise KernelError("verification_reverification_invalid", "不能替换已有复验授权")
                return
            if (
                sha256(owner._body).hexdigest() != plan.ledger_before_sha256
                or len(owner.period["requests"]) != plan.prior_request_count
            ):
                raise KernelError("verification_reverification_invalid", "授权不属于当前预算原件")
            try:
                validate_reverification_plan(owner.period, plan)
            except ValueError:
                raise KernelError(
                    "verification_reverification_invalid", "旧预留与授权不一致"
                ) from None
            if (
                _amount(owner.period["known_cost"])
                + _amount(owner.period["reserved_cost"])
                + _amount(plan.maximum_cost)
                > owner.allocation
            ):
                raise KernelError("verification_budget_exhausted", "原总预算不足以容纳复验上限")
            owner.period["bounded_reverification"] = plan.model_dump(mode="json")
            owner._save()

    @classmethod
    def rebind_reverification(cls, path: Path, binding: VerificationReverificationBinding) -> None:
        """可信管理宿主显式切换一次Suite；不执行模型IO，不增加原额度。"""
        try:
            checked = VerificationReverificationBinding.model_validate_json(
                binding.model_dump_json(), strict=True
            )
        except ValueError:
            raise KernelError("verification_reverification_invalid", "复验切换计划无效") from None
        owner = cls(path, checked.period_id)
        owner._registration_only = True
        with owner:
            existing = owner.reverification_binding
            if existing is not None:
                if existing != checked:
                    raise KernelError("verification_reverification_invalid", "不能替换已有复验切换")
                assert owner.root is not None
                try:
                    os.fsync(owner.root)
                except OSError:
                    raise KernelError(
                        "verification_budget_persist_failed", "复验切换未能可靠确认"
                    ) from None
                return
            plan = owner.reverification_plan
            if (
                plan is None
                or isinstance(plan, VerificationBetaTaskReverificationPlan)
                or sha256(owner._body).hexdigest() != checked.ledger_before_sha256
            ):
                raise KernelError("verification_reverification_invalid", "切换不属于原授权预算")
            if len(owner.period["requests"]) != checked.prior_request_count:
                raise KernelError("verification_reverification_invalid", "切换请求前缀已变化")
            try:
                validate_reverification_binding(owner.period, plan, checked)
            except ValueError:
                raise KernelError(
                    "verification_reverification_invalid", "切换金额或原件不一致"
                ) from None
            owner.period["reverification_binding"] = checked.model_dump(mode="json")
            # 旧Reader不理解Suite撤销；显式升版使其拒绝，而不是误用旧授权。
            owner.data["schema"] = _REBOUND_SCHEMA
            owner._save()

    def _validate(self, value: object) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("schema")
            not in {
                _SCHEMA,
                _REBOUND_SCHEMA,
                CHAIN_SCHEMA,
                TASK_CONTINUATION_SCHEMA,
                TASK_CONTINUATION_CHAIN_SCHEMA,
                USAGE_RECONCILIATION_SCHEMA,
            }
            or (value.get("provider"), value.get("currency")) != ("aliyun-bailian", "CNY")
        ):
            raise ValueError
        periods = value.get("periods")
        if not isinstance(periods, list):
            raise ValueError
        selected = [p for p in periods if isinstance(p, dict) and p.get("status") == "active"]
        if len(selected) != 1 or selected[0].get("period_id") != self.period_id:
            raise ValueError
        period = selected[0]
        requests = period.get("requests")
        if not isinstance(requests, list) or len(requests) > 10000:
            raise ValueError
        reconciled = reconciliation_costs(period)
        if value["schema"] == USAGE_RECONCILIATION_SCHEMA:
            if not reconciled:
                raise ValueError
        elif "usage_reconciliations" in period:
            raise ValueError
        known = reserved = 0
        identities: set[str] = set()
        for request in requests:
            identity = request["request_id"]
            UUID(identity)
            if identity in identities or request["status"] not in {
                "completed",
                "reserved",
                "unknown",
                "not_sent",
            }:
                raise ValueError
            identities.add(identity)
            held = _amount(request["reserved_cost"])
            if request["status"] in {"completed", "not_sent"}:
                if held:
                    raise ValueError
                known += _amount(request["cost_estimate"])
            elif request["status"] == "unknown" and identity in reconciled:
                known += reconciled[identity]
            elif held <= 0:
                raise ValueError
            if request["status"] == "reserved" or (
                request["status"] == "unknown" and identity not in reconciled
            ):
                reserved += held
        if known != _amount(period["known_cost"]) or reserved != _amount(period["reserved_cost"]):
            raise ValueError
        if known + reserved > _amount(period["allocation"]):
            raise ValueError
        raw_plan = period.get("bounded_reverification")
        if raw_plan is not None:
            plan = parse_reverification_plan(json.dumps(raw_plan))
            validate_reverification_plan(period, plan)
        elif any("reverification_id" in request or "task_id" in request for request in requests):
            raise ValueError
        raw_binding = period.get("reverification_binding")
        if value["schema"] in {_REBOUND_SCHEMA, CHAIN_SCHEMA}:
            if raw_binding is None or raw_plan is None:
                raise ValueError
            binding = VerificationReverificationBinding.model_validate_json(
                json.dumps(raw_binding), strict=True
            )
            if value["schema"] == CHAIN_SCHEMA:
                validate_candidate_chain(period, plan, binding, candidate_bindings(period))
            else:
                if "reverification_binding_chain" in period:
                    raise ValueError
                validate_reverification_binding(period, plan, binding)
        elif raw_binding is not None or any(
            "reverification_binding_id" in request for request in requests
        ):
            raise ValueError
        if value["schema"] == _SCHEMA and "reverification_binding_chain" in period:
            raise ValueError
        if value["schema"] in {
            TASK_CONTINUATION_SCHEMA,
            TASK_CONTINUATION_CHAIN_SCHEMA,
            USAGE_RECONCILIATION_SCHEMA,
        }:
            if raw_plan is None or "task_continuation" not in period:
                raise ValueError
            chain = task_continuation_chain(period)
            if value["schema"] in {TASK_CONTINUATION_SCHEMA, USAGE_RECONCILIATION_SCHEMA}:
                if "task_continuation_chain" in period:
                    raise ValueError
            elif not chain:
                raise ValueError
            record = (
                chain[-1]
                if chain
                else parse_task_continuation(json.dumps(period["task_continuation"]))
            )
            validate_task_continuation(period, plan, record)
        elif (
            "task_continuation" in period
            or "task_continuation_chain" in period
            or any("task_continuation_id" in request for request in requests)
        ):
            raise ValueError
        return value

    def _read(self) -> bytes:
        if self.root is None:
            raise OSError
        private_fs._private(os.fstat(self.root), directory=True)
        private_fs._private_acl(self.root)
        if self.lock is None:
            raise OSError
        lock_info = os.fstat(self.lock)
        private_fs._private(lock_info)
        private_fs._private_acl(self.lock)
        if private_fs._identity(lock_info) != private_fs._identity(
            os.stat(".provider-verification-budget.lock", dir_fd=self.root, follow_symlinks=False)
        ):
            raise OSError
        if self._root_identity != private_fs._directory_identity(self.path.parent.lstat()):
            raise OSError
        descriptor = os.open(
            self.path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=self.root,
        )
        try:
            before = os.fstat(descriptor)
            private_fs._private(before)
            private_fs._private_acl(descriptor)
            if before.st_size > _MAX_LEDGER_BYTES:
                raise OSError
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                body = stream.read(_MAX_LEDGER_BYTES + 1)
            current = os.stat(self.path.name, dir_fd=self.root, follow_symlinks=False)
            if len(body) != before.st_size or (
                private_fs._identity(before) != private_fs._identity(os.fstat(descriptor))
                or private_fs._identity(before) != private_fs._identity(current)
            ):
                raise OSError
            return body
        finally:
            os.close(descriptor)

    def _validate_continuation_save(self, original: dict[str, Any]) -> None:
        """承接登记只添加记录；之后冻结账本历史，只允许唯一新预留及其结算。"""
        old_period = next(p for p in original["periods"] if p["period_id"] == self.period_id)
        old_record = old_period.get("task_continuation")
        record = self.period.get("task_continuation")
        if old_record is None and record is None:
            return
        # 除活动周期的承接/请求/金额外，其余周期及顶层历史完全不可变。
        restored = {
            **self.data,
            "schema": original["schema"],
            "periods": [old_period if p is self.period else p for p in self.data["periods"]],
        }
        if restored != original:
            raise ValueError
        old_chain = old_period.get("task_continuation_chain", [])
        chain = self.period.get("task_continuation_chain", [])
        if chain != old_chain:
            if (
                not self._continuation_registration
                or self.data["schema"] != TASK_CONTINUATION_CHAIN_SCHEMA
                or len(chain) != len(old_chain) + 1
                or chain[:-1] != old_chain
                or chain[-1]["ledger_before_sha256"] != sha256(self._body).hexdigest()
                or chain[-1]["prior_request_count"] != len(old_period["requests"])
                or {k: v for k, v in self.period.items() if k != "task_continuation_chain"}
                != {k: v for k, v in old_period.items() if k != "task_continuation_chain"}
            ):
                raise ValueError
            return
        if old_record is None:
            if (
                not self._continuation_registration
                or record["ledger_before_sha256"] != sha256(self._body).hexdigest()
                or record["prior_request_count"] != len(old_period["requests"])
                or {k: v for k, v in self.period.items() if k != "task_continuation"} != old_period
            ):
                raise ValueError
            return
        if record != old_record or self.data["schema"] != original["schema"]:
            raise ValueError
        mutable = {"requests", "known_cost", "reserved_cost"}
        if {k: v for k, v in self.period.items() if k not in mutable} != {
            k: v for k, v in old_period.items() if k not in mutable
        }:
            raise ValueError
        old_requests, requests = old_period["requests"], self.period["requests"]
        if len(requests) < len(old_requests):
            raise ValueError
        if len(requests) > len(old_requests) and (
            len(requests) != len(old_requests) + 1
            or requests[-1]["status"] != "reserved"
            or "cost_estimate" in requests[-1]
            or "completed_at" in requests[-1]
        ):
            raise ValueError
        for before, after in zip(old_requests, requests, strict=False):
            if before == after:
                continue
            if before["status"] != "reserved" or not isinstance(after.get("completed_at"), str):
                raise ValueError
            expected = {**before, "status": after["status"], "completed_at": after["completed_at"]}
            if after["status"] in {"completed", "not_sent"}:
                cost = _amount(after["cost_estimate"])
                if cost > _amount(before["reserved_cost"]) or (
                    after["status"] == "not_sent" and cost
                ):
                    raise ValueError
                expected.update(reserved_cost="0", cost_estimate=after["cost_estimate"])
            elif after["status"] != "unknown":
                raise ValueError
            if after != expected:
                raise ValueError

    def _save(self) -> None:
        """原字节未变才发布自有候选；同步失败保留可能已发布预留，不发请求。"""
        assert self.root is not None
        temporary = f".budget-{uuid4().hex}.tmp"
        try:
            self._validate(self.data)
            original = self._validate(strict_json(self._body))
            if self._usage_reconciliation_registration:
                self._validate_usage_reconciliation_save(original)
            else:
                self._validate_continuation_save(original)
            original_period = next(
                p for p in original["periods"] if p["period_id"] == self.period_id
            )
            original_plan = original_period.get("bounded_reverification")
            if (
                original_plan is not None
                and self.period.get("bounded_reverification") != original_plan
            ):
                raise ValueError
            original_binding = original_period.get("reverification_binding")
            current_binding = self.period.get("reverification_binding")
            if original_binding is not None:
                if current_binding != original_binding:
                    raise ValueError
                old_chain = original_period.get("reverification_binding_chain", [])
                new_chain = self.period.get("reverification_binding_chain", [])
                if new_chain != old_chain:
                    if (
                        not self._candidate_registration
                        or self.data["schema"] != CHAIN_SCHEMA
                        or new_chain[:-1] != old_chain
                        or len(new_chain) != len(old_chain) + 1
                        or new_chain[-1]["ledger_before_sha256"] != sha256(self._body).hexdigest()
                        or {
                            k: v
                            for k, v in self.period.items()
                            if k != "reverification_binding_chain"
                        }
                        != {
                            k: v
                            for k, v in original_period.items()
                            if k != "reverification_binding_chain"
                        }
                    ):
                        raise ValueError
                elif self.data["schema"] != original["schema"]:
                    raise ValueError
            elif current_binding is not None and (
                current_binding["ledger_before_sha256"] != sha256(self._body).hexdigest()
                or self.period["requests"] != original_period["requests"]
            ):
                raise ValueError
            if self._read() != self._body or _amount(self.period["allocation"]) != self.allocation:
                raise ValueError
            body = (json.dumps(self.data, ensure_ascii=False, indent=2) + "\n").encode()
            if len(body) > _MAX_LEDGER_BYTES:
                raise ValueError
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.root,
            )
            with os.fdopen(descriptor, "wb") as stream:
                private_fs._private(os.fstat(stream.fileno()))
                private_fs._private_acl(stream.fileno())
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if self._read() != self._body:
                raise ValueError
            os.replace(temporary, self.path.name, src_dir_fd=self.root, dst_dir_fd=self.root)
            os.fsync(self.root)
            self._body = body
        except Exception:
            raise KernelError(
                "verification_budget_persist_failed", "验证预算未能可靠持久化"
            ) from None
        finally:
            try:
                os.unlink(temporary, dir_fd=self.root)
            except OSError:
                pass

    def _validate_usage_reconciliation_save(self, original: dict[str, Any]) -> None:
        """对账登记只能追加一条记录并按原预留精确调整累计金额。"""
        old_period = next(p for p in original["periods"] if p["period_id"] == self.period_id)
        old_records = old_period.get("usage_reconciliations", [])
        records = self.period.get("usage_reconciliations", [])
        if (
            not self._usage_reconciliation_registration
            or self.data["schema"] != USAGE_RECONCILIATION_SCHEMA
            or len(records) != len(old_records) + 1
            or records[:-1] != old_records
        ):
            raise ValueError
        record = usage_reconciliations(self.period)[-1]
        request = next(
            item for item in old_period["requests"] if item["request_id"] == str(record.request_id)
        )
        if (
            record.ledger_before_sha256 != sha256(self._body).hexdigest()
            or self.period["requests"] != old_period["requests"]
            or _amount(self.period["known_cost"])
            != _amount(old_period["known_cost"]) + _amount(record.cost_estimate)
            or _amount(self.period["reserved_cost"])
            != _amount(old_period["reserved_cost"]) - _amount(request["reserved_cost"])
        ):
            raise ValueError
        mutable = {"known_cost", "reserved_cost", "usage_reconciliations"}
        if {k: v for k, v in self.period.items() if k not in mutable} != {
            k: v for k, v in old_period.items() if k not in mutable
        }:
            raise ValueError
        restored = {
            **self.data,
            "schema": original["schema"],
            "periods": [old_period if p is self.period else p for p in self.data["periods"]],
        }
        if restored != original:
            raise ValueError

    def require_available(self) -> None:
        if self.root is None or self._registration_only:
            raise KernelError("verification_budget_unresolved", "验证预算存在未决请求")
        plan = self.reverification_plan
        binding = self.active_reverification_binding
        continuation = self.task_continuation
        allowed: set[str] = set()
        if continuation is not None or self.task_continuation_id is not None:
            if (
                continuation is None
                or self.task_continuation_id != continuation.continuation_id
                or self.reverification_id != continuation.reverification_id
                or self.task_id != continuation.task_id
                or self.suite_id is not None
                or plan is None
            ):
                raise KernelError("verification_budget_unresolved", "任务承接身份不匹配")
            validate_task_continuation(self.period, plan, continuation)
            if (
                continuation.maximum_requests is not None
                and len(self.period["requests"]) - continuation.prior_request_count
                >= continuation.maximum_requests
            ):
                raise KernelError("verification_budget_unresolved", "任务承接请求次数已耗尽")
        if isinstance(plan, VerificationBetaTaskReverificationPlan) or any(
            identity is not None
            for identity in (self.reverification_id, self.suite_id, self.task_id)
        ):
            if plan is None or self.reverification_id != plan.reverification_id:
                raise KernelError("verification_budget_unresolved", "复验身份与持久授权不一致")
            if isinstance(plan, VerificationBetaTaskReverificationPlan):
                matched = self.task_id == plan.task_id and self.suite_id is None and binding is None
            else:
                matched = self.task_id is None and self.suite_id == (
                    binding.suite_id if binding is not None else plan.suite_id
                )
            if not matched:
                raise KernelError("verification_budget_unresolved", "复验身份与持久授权不一致")
            validate_reverification_plan(self.period, plan)
            allowed = {str(r.request_id) for r in plan.carried_requests}
            if continuation is not None:
                allowed = {str(r.request_id) for r in continuation.carried_requests}
        if any(
            request["status"] == "reserved"
            or (
                request["status"] == "unknown"
                and request["request_id"] not in allowed
                and request["request_id"] not in reconciliation_costs(self.period)
            )
            for request in self.period["requests"]
        ):
            raise KernelError("verification_budget_unresolved", "验证预算存在未决请求")

    def reserve(self, maximum_units: int, metadata: dict[str, Any]) -> UUID:
        """保留原周期和历史；不自动加额、清零或猜测未决金额。"""
        self.require_available()
        if (
            set(metadata)
            - {
                "purpose",
                "requested_model",
                "region",
                "max_output_tokens",
                "max_attempts",
                "thread_id",
                "turn_id",
                "step",
                "price_basis",
            }
            or type(maximum_units) is not int
        ):
            raise KernelError("verification_budget_metadata_invalid", "验证预算元数据不受支持")
        period = self.period
        plan = self.reverification_plan
        binding = self.active_reverification_binding
        if plan is not None and (
            validate_reverification_plan(period, plan) + maximum_units > _amount(plan.maximum_cost)
        ):
            raise KernelError("verification_budget_exhausted", "下一请求超出单轮复验上限")
        if maximum_units <= 0 or (
            _amount(period["known_cost"]) + _amount(period["reserved_cost"]) + maximum_units
            > self.allocation
        ):
            raise KernelError("verification_budget_exhausted", "验证预算不足以预留下一请求")
        request_id = uuid4()
        period["requests"].append(
            {
                **metadata,
                **({"reverification_id": str(plan.reverification_id)} if plan is not None else {}),
                **(
                    {"task_id": plan.task_id}
                    if isinstance(plan, VerificationBetaTaskReverificationPlan)
                    else {}
                ),
                **(
                    {"task_continuation_id": str(self.task_continuation_id)}
                    if self.task_continuation_id is not None
                    else {}
                ),
                **(
                    {
                        "reverification_binding_id": str(binding.binding_id),
                        "suite_id": str(binding.suite_id),
                    }
                    if binding is not None
                    else {}
                ),
                "request_id": str(request_id),
                "started_at": datetime.now(UTC).isoformat(),
                "status": "reserved",
                "reserved_cost": format_amount(maximum_units),
            }
        )
        period["reserved_cost"] = format_amount(_amount(period["reserved_cost"]) + maximum_units)
        self._save()
        return request_id

    def settle(self, request_id: UUID, cost_units: int | None, *, sent: bool) -> None:
        """完整已知费用释放差额；发送后的未知费用继续占用全部预留。"""
        request = next(r for r in self.period["requests"] if r["request_id"] == str(request_id))
        held = _amount(request["reserved_cost"])
        if (
            request["status"] != "reserved"
            or type(sent) is not bool
            or (
                cost_units is not None
                and (type(cost_units) is not int or not 0 <= cost_units <= held)
            )
            or (not sent and cost_units not in {None, 0})
        ):
            raise KernelError("verification_budget_settlement_invalid", "验证预算结算与预留不一致")
        if sent and cost_units is None:
            request["status"] = "unknown"
        else:
            request.update(
                status="completed" if sent else "not_sent",
                reserved_cost="0",
                cost_estimate=format_amount(cost_units or 0),
            )
            self.period["reserved_cost"] = format_amount(
                _amount(self.period["reserved_cost"]) - held
            )
            self.period["known_cost"] = format_amount(
                _amount(self.period["known_cost"]) + (cost_units or 0)
            )
        request["completed_at"] = datetime.now(UTC).isoformat()
        self._save()
