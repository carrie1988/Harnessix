"""Harbor任务容器内Harnessix的原生配置生成器。

模型与命令环境沿用一期本地组别（预设见task_environment）：同一本地模型、关闭思考、
同一按语言的环境预设与900秒命令期限。命令改走外部边界原生Profile（ADR 0121）：任务容器即边界，
工作区可写且改动保留。模型经协议运行器在容器回环端口上的转发访问宿主模型服务。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_native_process_profile,
)
from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config, thinking_preset
from integrations.harbor.task_environment import API_KEY_VARIABLE, MODEL, environment_presets

COMMAND_TIMEOUT_SECONDS = 900
# 宿主模型服务的上下文窗口（vLLM --max-model-len）；未列出的模型沿用产品固定上限。
CONTEXT_WINDOW_TOKENS = {MODEL: 262_144}
EVAL_ALLOWED_OVERRIDES = frozenset({"completion_guard"})
# 评测每步输出上限：自托管默认4096会让偶发的一步长推导截断整题（dev25 xarray-4356），评测放宽到
# 16384；非思考约一个流式分块/Token，分块与响应字节上限同步放宽。这是已知的评测与产品差异。
EVAL_PROFILE_OVERRIDES: Mapping[str, object] = {
    "max_output_tokens": 16_384,
    "max_chunks": 20_000,
    "max_response_bytes": 4 * 1024 * 1024,
}


def apply_eval_overrides(
    config: ProductConfigV2, overrides: Mapping[str, object]
) -> ProductConfigV2:
    """评测配置必须来自产品向导，只能覆盖白名单字段，保证测到的就是用户拿到的配置。"""

    rejected = sorted(set(overrides) - EVAL_ALLOWED_OVERRIDES)
    if rejected:
        raise SystemExit(f"评测配置覆盖了白名单外的字段：{rejected}")
    updated = config.model_copy(update=dict(overrides)).model_dump_json()
    return type(config).model_validate_json(updated)


def apply_eval_profile_overrides(
    config: ProductConfigV2, overrides: Mapping[str, object]
) -> ProductConfigV2:
    """活动Profile只能覆盖EVAL_PROFILE_OVERRIDES登记的字段，结果仍经产品合同完整校验。"""

    rejected = sorted(set(overrides) - set(EVAL_PROFILE_OVERRIDES))
    if rejected:
        raise SystemExit(f"评测配置覆盖了白名单外的Profile字段：{rejected}")
    profiles = tuple(
        profile.model_copy(update=dict(overrides))
        if profile.profile_id == config.active_profile
        else profile
        for profile in config.profiles
    )
    updated = config.model_copy(update={"profiles": profiles}).model_dump_json()
    return type(config).model_validate_json(updated)


def product_config(relay_port: int, *, model: str = MODEL, thinking: str = "off") -> str:
    base_url = f"http://127.0.0.1:{relay_port}/v1"
    preset = thinking_preset(base_url, model)
    if preset is None:
        raise SystemExit("回环端点没有思考协议预设")
    draft = ConfigurationDraft(
        provider_kind="openai_chat",
        base_url=base_url,
        model=model,
        environment_variable=API_KEY_VARIABLE,
        thinking_protocol=preset[0],
        omit_tool_choice=preset[1],
        thinking=thinking,
        context_window_tokens=CONTEXT_WINDOW_TOKENS.get(model),
    )
    # 协议运行器不经过`harnessix exec`，无人值守的完成守卫要在配置里开启。
    config = apply_eval_overrides(build_product_config(draft), {"completion_guard": True})
    config = apply_eval_profile_overrides(config, EVAL_PROFILE_OVERRIDES)
    return config.model_dump_json(indent=2)


def action_config(language: str = "python") -> str:
    profile = build_product_native_process_profile(
        profile_id="testbed",
        version="harbor-external-boundary-v1",
        description="在本任务容器中运行命令（工作区/testbed，改动保留）",
        backend="external_boundary",
        program="/bin/bash",
        arguments=("-c",),
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        environment=environment_presets(language),
    )
    return build_product_action_config(native_process_profiles=(profile,)).model_dump_json(indent=2)


def _write(path: Path, body: str) -> None:
    descriptor = path.open("x", encoding="utf-8")
    with descriptor:
        descriptor.write(body)
    path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay-port", type=int, required=True)
    parser.add_argument("--language", default="python")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(args.output_dir / "product.json", product_config(args.relay_port))
    _write(args.output_dir / "actions.json", action_config(args.language))
    print(json.dumps({"reason": "written", "directory": str(args.output_dir)}))


if __name__ == "__main__":
    main()
