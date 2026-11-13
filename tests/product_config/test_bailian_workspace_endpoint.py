"""阿里云百炼北京的业务空间专属域名与共享域名用同一套核对表（Beta Issue #6）。

0.10.0b25 Windows实测：`--base-url https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
配置qwen3.8-flash后，底栏一直显示“费用未知”，向导也不再显示单价。阿里云文档推荐生产环境使用业务空间
专属域名（`{WorkspaceId}.{地域}.maas.aliyuncs.com`）；牌价、上下文窗口与思考预设此前只认`dashscope.aliyuncs.com`。
只有北京地域（cn-beijing）与共享域名同属一个地域，其他地域的牌价与型号不同，不归并。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from harnessix.product_config.reasoning_models import reasons_by_default
from harnessix.product_config.vendors import endpoint_host
from harnessix.product_config.wizard import (
    effort_tiers_verified,
    known_context_window,
    thinking_preset,
)
from harnessix.product_ui.cost_estimate import list_price_for

SHARED = "https://dashscope.aliyuncs.com/compatible-mode/v1"
WORKSPACE = "https://llm-abc123def.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.mark.parametrize(
    ("base_url", "host"),
    [
        (SHARED, "dashscope.aliyuncs.com"),
        (WORKSPACE, "dashscope.aliyuncs.com"),
        (
            "https://LLM-ABC.CN-BEIJING.MAAS.ALIYUNCS.COM/compatible-mode/v1",
            "dashscope.aliyuncs.com",
        ),
        # 其他地域另有牌价与型号，不归并。
        ("https://llm-abc.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", None),
        # 只认正好一段业务空间标识：多一段或少一段都不是专属域名。
        ("https://a.b.cn-beijing.maas.aliyuncs.com/v1", None),
        ("https://cn-beijing.maas.aliyuncs.com/v1", None),
        ("https://evil.example/llm-abc.cn-beijing.maas.aliyuncs.com", None),
        ("https://api.deepseek.com/v1", "api.deepseek.com"),
    ],
)
def test_the_beijing_workspace_domain_is_looked_up_as_bailian(base_url: str, host) -> None:
    expected = host or base_url.split("/")[2].lower()
    assert endpoint_host(base_url) == expected


def test_the_workspace_domain_gets_the_bailian_price_window_and_thinking_presets() -> None:
    price = list_price_for(WORKSPACE, "qwen3.8-flash")
    assert price is not None
    assert (price.input_per_million, price.output_per_million) == (Decimal("0.8"), Decimal("2.7"))
    assert known_context_window(WORKSPACE, "qwen3.8-flash") == 1_000_000
    assert thinking_preset(WORKSPACE, "qwen3.8-flash") == thinking_preset(SHARED, "qwen3.8-flash")
    assert effort_tiers_verified(WORKSPACE, "qwen3.8-flash")
    assert reasons_by_default(WORKSPACE, "qwen3.8-max")


def test_other_regions_stay_unknown() -> None:
    singapore = "https://llm-abc.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    assert list_price_for(singapore, "qwen3.8-flash") is None
    assert known_context_window(singapore, "qwen3.8-flash") is None
