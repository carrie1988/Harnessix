"""在原 Workspace CAS 耐久保存完整 Core；内容地址不授予归属或执行权限。

Core 在 Route 之前保存，唯一地址为排除自身 fingerprint 的完整规范正文 SHA。
本入口只调用原 put_blob/blob，不增建 SQL、索引、签名、批准或成功事件。
取消或回读失败可能留下合法 CAS 孤儿；调用方不得据此登记业务成功或删除正文。
完整材料、父历史、原 Session、MAC、Owner 及执行授权仍由其原边界独立复核。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import pure_git_authentication
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_delivery_observed_wire import (
    decode_product_git_delivery_core_v2,
    encode_product_git_delivery_core_v2,
)
from harnessix.product_config.git_delivery_plan_contracts import ProductGitDeliveryCore
from harnessix.product_config.git_delivery_plan_snapshot import (
    invalid_git_delivery_plan,
    snapshot_product_git_delivery_core,
    snapshot_product_git_delivery_core_v2,
)
from harnessix.product_config.git_delivery_plan_wire import (
    MAX_PRODUCT_GIT_PLAN_BYTES,
    decode_product_git_delivery_core,
    encode_product_git_delivery_core,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _invalid_store() -> KernelError:
    """固定公开错误不暴露原 Store 路径、配置或输入正文。"""
    return KernelError("git_delivery_core_store_invalid", "Git交付Core原CAS入口无效")


def _io_error(writing: bool) -> KernelError:
    """只暴露有限 IO 分类，不记录第三方错误、作者、消息或对象正文。"""
    if writing:
        return KernelError("git_delivery_core_write_failed", "Git交付Core原CAS持久化失败")
    return KernelError("git_delivery_core_read_failed", "Git交付Core原CAS读取失败")


def _store(value: object) -> SQLiteWorkspaceTransactionStore:
    """原 Store 必须为确切实际类型，不接受子类、代理或鸭子端口。"""
    if type(value) is not SQLiteWorkspaceTransactionStore:
        raise _invalid_store()
    return value


def _fingerprint(value: object) -> str:
    """先验证确切的小写 SHA 字符串，非法摘要不能进入原固定路径端口。"""
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _io_error(False)
    return value


class _Checkpoint:
    """记住调用方检查点原异常，防止 IO/解析器同类型错误遮蔽取消或超时。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.error: BaseException | None = None

    def __call__(self) -> None:
        try:
            self.callback()
        except BaseException as error:
            self.error = error
            raise


def _body(value: object, digest: str) -> bytes:
    """读回完整确切 bytes；512KiB、原始 SHA 和规范解码均不得放宽。"""
    if (
        type(value) is not bytes
        or not 1 <= len(value) <= MAX_PRODUCT_GIT_PLAN_BYTES
        or hashlib.sha256(value).hexdigest() != digest
    ):
        raise _io_error(False)
    return value


def _persist[T: ProductGitDeliveryCore | ProductGitDeliveryCoreV2](
    source_store: object, core: object, kind: type[T], checkpoint: Callable[[], None]
) -> T:
    """两代共用唯一原耐久写/读回算法，规范字节与内容地址全部核验。"""
    check = _Checkpoint(checkpoint)
    check()
    store = _store(source_store)
    with pure_git_authentication(checkpoint) as pure_check:
        pure = _Checkpoint(pure_check)
        snapshot = (
            snapshot_product_git_delivery_core(core, checkpoint=pure)
            if kind is ProductGitDeliveryCore
            else snapshot_product_git_delivery_core_v2(core, checkpoint=pure)
        )
        body = (
            encode_product_git_delivery_core(snapshot, checkpoint=pure)
            if kind is ProductGitDeliveryCore
            else encode_product_git_delivery_core_v2(snapshot, checkpoint=pure)
        )
    if hashlib.sha256(body).hexdigest() != snapshot.fingerprint:
        raise invalid_git_delivery_plan()
    try:
        check()
        store.put_blob(snapshot.fingerprint, body, checkpoint=check)
        check()
        actual = store.blob(snapshot.fingerprint, checkpoint=check)
        check()
        if _body(actual, snapshot.fingerprint) != body:
            raise _io_error(True)
    except UpstreamCheckpointError as error:
        if check.error is not None:
            raise check.error from None
        # 原 Store 显式标记实际检查点，解一层保留原控制身份，不按 code 放行。
        raise error.error from None
    except Exception:
        if check.error is not None:
            raise check.error from None
        raise _io_error(True) from None

    # 只在实际 CAS 回读及正文核验之后解析；纯段边界不进入 IO 错误映射。
    with pure_git_authentication(checkpoint) as pure_check:
        check = _Checkpoint(pure_check)
        try:
            result = (
                decode_product_git_delivery_core(actual, checkpoint=check)
                if kind is ProductGitDeliveryCore
                else decode_product_git_delivery_core_v2(actual, checkpoint=check)
            )
            check()
            return cast(T, result)
        except UpstreamCheckpointError as error:
            if check.error is not None:
                raise check.error from None
            raise error.error from None
        except Exception:
            if check.error is not None:
                raise check.error from None
            raise _io_error(True) from None


def _load[T: ProductGitDeliveryCore | ProductGitDeliveryCoreV2](
    source_store: object, fingerprint: object, kind: type[T], checkpoint: Callable[[], None]
) -> T:
    """两代共享原固定地址和完整读算法；不新增Store、SQL或授权用途。"""
    check = _Checkpoint(checkpoint)
    check()
    store = _store(source_store)
    digest = _fingerprint(fingerprint)
    try:
        check()
        body = store.blob(digest, checkpoint=check)
        check()
        actual = _body(body, digest)
    except UpstreamCheckpointError as error:
        if check.error is not None:
            raise check.error from None
        raise error.error from None
    except Exception:
        if check.error is not None:
            raise check.error from None
        raise _io_error(False) from None

    # CAS I/O 已完成；只给原纯解析器局部频检，不把原 Store 放入纯段。
    with pure_git_authentication(checkpoint) as pure_check:
        check = _Checkpoint(pure_check)
        try:
            result = (
                decode_product_git_delivery_core(actual, checkpoint=check)
                if kind is ProductGitDeliveryCore
                else decode_product_git_delivery_core_v2(actual, checkpoint=check)
            )
            check()
            return cast(T, result)
        except Exception:
            if check.error is not None:
                raise check.error from None
            raise _io_error(False) from None


@dataclass(frozen=True, slots=True)
class ProductGitDeliveryCoreStore:
    """受信宿主原 CAS 的有限 Core 入口；不核验或推断宿主业务权威。"""

    store: SQLiteWorkspaceTransactionStore = field(repr=False)

    def __post_init__(self) -> None:
        _store(self.store)

    def persist(self, core: object, *, checkpoint: Callable[[], None]) -> ProductGitDeliveryCore:
        """完整快照、规范编码、原 CAS 写入及精确回读全部通过才返回新 Core。"""
        return _persist(self.store, core, ProductGitDeliveryCore, checkpoint)

    def persist_v2(
        self, core: object, *, checkpoint: Callable[[], None]
    ) -> ProductGitDeliveryCoreV2:
        """全用户观察进入同一原CAS；旧Core1不会在新入口被升级。"""
        return _persist(self.store, core, ProductGitDeliveryCoreV2, checkpoint)

    def load(
        self, fingerprint: object, *, checkpoint: Callable[[], None]
    ) -> ProductGitDeliveryCore:
        """按唯一原内容地址完整恢复新快照，不把 CAS 摘要当作归属或批准证明。"""
        return _load(self.store, fingerprint, ProductGitDeliveryCore, checkpoint)

    def load_v2(
        self, fingerprint: object, *, checkpoint: Callable[[], None]
    ) -> ProductGitDeliveryCoreV2:
        """只恢复完整Core2；缺完整U观察的旧记录不能补签或新代际解读。"""
        return _load(self.store, fingerprint, ProductGitDeliveryCoreV2, checkpoint)
