"""Harbor任务容器内Harnessix的原生配置生成器。

模型与命令环境沿用一期本地组别（scripts/swebench_local_run.py）：同一本地模型、关闭思考、
同一按语言的环境预设与900秒命令期限。命令改走外部边界原生Profile（ADR 0121）：任务容器即边界，
工作区可写且改动保留。模型经协议运行器在容器回环端口上的转发访问宿主模型服务。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_native_process_profile,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config, thinking_preset
from scripts.swebench_local_run import API_KEY_VARIABLE, MODEL, environment_presets

COMMAND_TIMEOUT_SECONDS = 900
# 宿主模型服务的上下文窗口（vLLM --max-model-len）；未列出的模型沿用产品固定上限。
CONTEXT_WINDOW_TOKENS = {MODEL: 262_144}


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
    return build_product_config(draft).model_dump_json(indent=2)


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
