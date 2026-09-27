"""用户可控持久输入的纯保护边界；复用同一原材料，保持输入错误命名空间。"""

from __future__ import annotations

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import PublicOutputProtection, protect_json

INPUT_FAILURE_CODES = {
    "public_output_secret_leak": "public_input_secret_leak",
    "public_output_secret_unavailable": "public_input_secret_unavailable",
    "public_output_limit": "public_input_limit",
    "public_output_timeout": "public_input_timeout",
    "public_output_protection_failed": "public_input_protection_failed",
}


async def protect_input(
    protection: PublicOutputProtection | None, value: JsonValue, cancel: CancelToken
) -> None:
    """不替换原输入或身份；失败必须先于Claim、Session事务和批准副作用。"""
    try:
        await protect_json(protection, value, cancel)
    except KernelError as error:
        code = INPUT_FAILURE_CODES.get(error.code, "public_input_protection_failed")
        raise KernelError(code, "输入未通过公开数据保护校验") from None
