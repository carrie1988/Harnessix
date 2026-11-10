"""Windows原生Git观察合同在活动树上的核对范围。"""

from __future__ import annotations

# pyproject.toml随依赖持续变化；活动树只核对冻结合同里的Git执行源码。
# 重新运行Windows原生观察前须按新候选重新冻结合同，届时pyproject.toml按新字节绑定。
DEPENDENCY_MANIFEST = "pyproject.toml"


def live_execution_sources(contract: dict) -> dict:
    """去掉依赖清单行后的合同；其余17项源码仍须与冻结字节完全一致。"""

    return {
        **contract,
        "source_inputs": [
            row for row in contract["source_inputs"] if row["path"] != DEPENDENCY_MANIFEST
        ],
    }
