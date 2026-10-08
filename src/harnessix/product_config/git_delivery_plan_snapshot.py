"""Git 计划入口的实际类型深层重建；拒绝序列化前的伪造、子类和额外字段。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_inventory_contracts import (
    GitInventoryScope,
    snapshot_git_inventory_scope,
)
from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
)


def invalid_git_delivery_plan() -> KernelError:
    """固定公开错误不包含路径、作者、消息、对象正文或底层解析器输出。"""
    return KernelError("git_delivery_plan_invalid", "Git交付完整计划不符合契约")


def _json_value(value: object, checkpoint: Callable[[], None]) -> object:
    """原 JsonValue 仅允许原生 JSON 容器和标量，不隐式转换任意对象。"""
    checkpoint()
    if type(value) in {str, int, float, bool, type(None)}:
        return value
    if type(value) is list:
        return [_json_value(item, checkpoint) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _json_value(item, checkpoint) for key, item in value.items()}
    raise invalid_git_delivery_plan()


def _matches(value: object, annotation: object) -> bool:
    """只判断分派外形；完整校验仍由原模型和严格字段递归负责。"""
    if annotation is JsonValue:
        return True
    if annotation is AwareDatetime:
        return type(value) is datetime
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _matches(value, args[0])
    if origin is Literal:
        return any(type(value) is type(item) and value == item for item in args)
    return type(value) is (origin or annotation)


def _field(value: object, annotation: object, checkpoint: Callable[[], None]) -> object:
    """按已声明字段分派，保留 UUID、Enum、日期、tuple 的实际类型。"""
    checkpoint()
    if annotation is JsonValue:
        # 只接受原框架 JsonValue 的确切别名，不以别名名称扩大输入类型。
        return _json_value(value, checkpoint)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _field(value, args[0], checkpoint)
    if origin in {UnionType, Union}:
        candidates = [item for item in args if _matches(value, item)]
        if len(candidates) != 1:
            raise invalid_git_delivery_plan()
        return _field(value, candidates[0], checkpoint)
    if not _matches(value, annotation):
        raise invalid_git_delivery_plan()
    if origin is Literal or annotation in {
        str,
        int,
        float,
        bool,
        type(None),
        UUID,
        datetime,
        AwareDatetime,
    }:
        return value
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return value
    if origin is tuple:
        return tuple(_field(item, args[0], checkpoint) for item in cast(tuple[object, ...], value))
    if origin is dict:
        return {
            _field(key, args[0], checkpoint): _field(item, args[1], checkpoint)
            for key, item in cast(dict[object, object], value).items()
        }
    if annotation is GitInventoryScope:
        return snapshot_git_inventory_scope(value, checkpoint=checkpoint)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model(value, annotation, checkpoint)
    raise invalid_git_delivery_plan()


def _model[T: BaseModel](value: object, kind: type[T], checkpoint: Callable[[], None]) -> T:
    """从原字段深重建；不相信 model_copy、model_construct 或 serializer 的规范化。"""
    checkpoint()
    if type(value) is not kind:
        raise invalid_git_delivery_plan()
    current = value
    if set(vars(current)) != set(kind.model_fields) or current.__pydantic_extra__ is not None:
        raise invalid_git_delivery_plan()
    fields = {
        name: _field(getattr(current, name), field.annotation, checkpoint)
        for name, field in kind.model_fields.items()
    }
    result = kind.model_validate(fields, context={"checkpoint": checkpoint})
    checkpoint()
    return result


def snapshot_product_git_delivery_core(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryCore:
    """深层严格快照只核对声明；不观察 CAS、原 Session 或新批准。"""
    return _snapshot(value, ProductGitDeliveryCore, checkpoint)


def snapshot_product_git_delivery_plan(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryPlan:
    """返回新的完整封套，阻止修改调用 arguments 的旧别名改变执行计划。"""
    return _snapshot(value, ProductGitDeliveryPlan, checkpoint)


def snapshot_product_git_delivery_core_v2(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryCoreV2:
    """显式完整Core2深重建；不对旧Core1补观察或升级。"""
    return _snapshot(value, ProductGitDeliveryCoreV2, checkpoint)


def snapshot_product_git_delivery_plan_v2(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryPlanV2:
    """显式Plan2完整快照，原四个入口共用唯一严格递归算法。"""
    return _snapshot(value, ProductGitDeliveryPlanV2, checkpoint)


def _snapshot[T: BaseModel](value: object, kind: type[T], checkpoint: Callable[[], None]) -> T:
    """检查点异常保留原身份，即使其类型与解析器错误相同也不得重新分类。"""
    if type(checkpoint) is GitAuthenticationControl:
        # 只包装原纯算法；边界认证异常位于解析器收敛之外，保持原对象。
        with checkpoint.pure() as pure_check:
            return _snapshot(value, kind, pure_check)
    callback_error: BaseException | None = None

    def check() -> None:
        nonlocal callback_error
        try:
            checkpoint()
        except BaseException as error:
            callback_error = error
            raise

    try:
        return _model(value, kind, check)
    except (KernelError, ValidationError, AttributeError, TypeError, ValueError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise invalid_git_delivery_plan() from None
