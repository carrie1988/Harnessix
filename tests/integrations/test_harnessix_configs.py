"""Harbor任务容器内Harnessix配置生成：不依赖Harbor包，本地全量回归即可覆盖。"""

from __future__ import annotations

import json

from integrations.harbor.harnessix_configs import CONTEXT_WINDOW_TOKENS, product_config
from scripts.swebench_local_run import MODEL


def test_harbor_product_config_declares_the_local_model_window() -> None:
    profile = json.loads(product_config(18001))["profiles"][0]
    assert profile["context_window_tokens"] == CONTEXT_WINDOW_TOKENS[MODEL] == 262_144
    other = json.loads(product_config(18001, model="other-model"))["profiles"][0]
    assert "context_window_tokens" not in other
