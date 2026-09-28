"""真实Provider验证宿主的持久预留账本；不创建预算、不保存凭据或正文。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.models._json import strict_json
from harnessix.models.pricing import amount_units, format_amount
from harnessix.product_config import session_key_posix as private_fs

_MAX_LEDGER_BYTES = 1024 * 1024
_SCHEMA = "harnessix.provider-verification-budget/v1"


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

    def __init__(self, path: Path, period_id: UUID) -> None:
        self.path = path.absolute()
        self.period_id = str(period_id)
        self.root: int | None = None
        self.lock: int | None = None
        self._body = b""
        self._root_identity: tuple[int, ...] = ()
        self.data: dict[str, Any] = {}
        self.allocation = 0

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

    def _validate(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or (
            value.get("schema"),
            value.get("provider"),
            value.get("currency"),
        ) != (_SCHEMA, "aliyun-bailian", "CNY"):
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
            elif held <= 0:
                raise ValueError
            reserved += held
        if known != _amount(period["known_cost"]) or reserved != _amount(period["reserved_cost"]):
            raise ValueError
        if known + reserved > _amount(period["allocation"]):
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

    def _save(self) -> None:
        """原字节未变才发布自有候选；同步失败保留可能已发布预留，不发请求。"""
        assert self.root is not None
        temporary = f".budget-{uuid4().hex}.tmp"
        try:
            self._validate(self.data)
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

    def require_available(self) -> None:
        if self.root is None or any(
            request["status"] in {"reserved", "unknown"} for request in self.period["requests"]
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
        if maximum_units <= 0 or (
            _amount(period["known_cost"]) + _amount(period["reserved_cost"]) + maximum_units
            > self.allocation
        ):
            raise KernelError("verification_budget_exhausted", "验证预算不足以预留下一请求")
        request_id = uuid4()
        period["requests"].append(
            {
                **metadata,
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
