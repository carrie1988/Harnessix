"""旧配置在核实过思考档位的模型上默认按中档思考：运行时补齐，不改配置文件。

0.10.0b23起向导为百炼qwen3.8-max与qwen3.8-flash写入思考协议和中档强度（ADR 0146）。
更早的配置要么没有思考协议、要么没有强度，模型都按服务端默认的最强档思考：负责人本机一个任务的
第一步想了11分23秒、用满32,768个输出Token，仍没有写出任何内容。此前只在启动时提示重新运行configure，
很容易被忽略。现在运行时按新配置的写法补上；显式设置过强度、思考上限或关闭思考的配置不受影响
（负责人2026-11-13决定）。
"""

from __future__ import annotations

from typing import Final

from harnessix.product_config.contracts import ModelProfile, ProviderDefinition
from harnessix.product_config.wizard import effort_tiers_verified, thinking_preset

DEFAULT_THINKING_EFFORT: Final = "medium"


def with_default_thinking_effort(
    definition: ProviderDefinition, profile: ModelProfile
) -> tuple[ProviderDefinition, ModelProfile]:
    """没有设置思考强度与上限、也没有关闭思考的旧配置，在已核实档位的模型上按中档；其余原样返回。"""

    if (
        profile.thinking_effort is not None
        or profile.thinking_budget_tokens is not None
        or profile.thinking == "off"
        or definition.self_hosted
        or not effort_tiers_verified(definition.base_url, profile.model)
    ):
        return definition, profile
    preset = thinking_preset(definition.base_url, profile.model)
    if preset is None or definition.thinking_protocol not in (None, preset[0]):
        return definition, profile
    if definition.thinking_protocol is None:
        protocol, omit_tool_choice = preset
        definition = definition.model_copy(
            update={"thinking_protocol": protocol, "omit_tool_choice": omit_tool_choice}
        )
    return definition, profile.model_copy(update={"thinking_effort": DEFAULT_THINKING_EFFORT})
