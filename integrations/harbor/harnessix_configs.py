"""Harbor任务容器内Harnessix的原生配置生成器。

模型与命令环境沿用一期本地组别（预设见task_environment）：同一本地模型、关闭思考、
同一按语言的环境预设与900秒命令期限。命令改走外部边界原生Profile（ADR 0121）：任务容器即边界，
工作区可写且改动保留。模型经协议运行器在容器回环端口上的转发访问宿主模型服务。

两种用法：在宿主机上用集成包Python生成product.json与固定的actions.json（静态预设）；或由适配器
上传到任务容器，用容器内同一集成包按容器自身环境与任务语言生成本试次的actions.json（任务环境）。
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
from harnessix.sandbox.contracts import container_environment_problem

try:  # 宿主机上按仓库路径导入；适配器把两个文件并排上传到容器后按同目录导入
    from integrations.harbor.task_environment import (
        API_KEY_VARIABLE,
        MODEL,
        environment_presets,
        parse_environ,
        task_command_environment,
    )
except ImportError:  # pragma: no cover - 容器内与task_environment.py同目录运行
    from task_environment import (  # type: ignore[no-redef]
        API_KEY_VARIABLE,
        MODEL,
        environment_presets,
        parse_environ,
        task_command_environment,
    )

COMMAND_TIMEOUT_SECONDS = 900
# 宿主模型服务的上下文窗口（vLLM --max-model-len）；未列出的模型沿用产品固定上限。
CONTEXT_WINDOW_TOKENS = {MODEL: 262_144}
EVAL_ALLOWED_OVERRIDES = frozenset({"completion_guard"})


def apply_eval_overrides(
    config: ProductConfigV2, overrides: Mapping[str, object]
) -> ProductConfigV2:
    """评测配置必须来自产品向导，只能覆盖白名单字段，保证测到的就是用户拿到的配置。"""

    rejected = sorted(set(overrides) - EVAL_ALLOWED_OVERRIDES)
    if rejected:
        raise SystemExit(f"评测配置覆盖了白名单外的字段：{rejected}")
    updated = config.model_copy(update=dict(overrides)).model_dump_json()
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
    return config.model_dump_json(indent=2)


def action_config(
    language: str = "python", environment: tuple[tuple[str, str], ...] | None = None
) -> str:
    """命令环境默认是该语言的静态预设；给出environment时用它（任务环境）。"""
    profile = build_product_native_process_profile(
        profile_id="testbed",
        version="harbor-external-boundary-v1",
        description="在本任务容器中运行命令（工作区/testbed，改动保留）",
        backend="external_boundary",
        program="/bin/bash",
        arguments=("-c",),
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        environment=environment_presets(language) if environment is None else environment,
    )
    return build_product_action_config(native_process_profiles=(profile,)).model_dump_json(indent=2)


def task_action_config(container: Mapping[str, str], language: str) -> tuple[str, list[str]]:
    """按容器自身环境与任务语言生成actions.json，并返回因名称不能声明而略去的变量。

    产品不允许预设的名称（代理变量等受管名称、HARNESSIX_前缀、非法名称）略去并如实返回；
    值不合规（超长、多行）不略去，由Profile校验报错，宁可本试次安装失败也不悄悄改环境。
    """
    environment = task_command_environment(container, language)
    dropped = [name for name, _ in environment if container_environment_problem(((name, ""),))]
    kept = tuple(item for item in environment if item[0] not in dropped)
    return action_config(language, kept), dropped


def _write(path: Path, body: str) -> None:
    descriptor = path.open("x", encoding="utf-8")
    with descriptor:
        descriptor.write(body)
    path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="python")
    parser.add_argument("--relay-port", type=int, help="静态用法：生成product.json与actions.json")
    parser.add_argument("--output-dir", type=Path, help="静态用法的输出目录")
    parser.add_argument(
        "--container-environment", type=Path, help="任务环境用法：容器自身环境，如/proc/1/environ"
    )
    parser.add_argument("--actions-file", type=Path, help="任务环境用法的actions.json路径")
    args = parser.parse_args()
    if args.container_environment is not None and args.actions_file is not None:
        container = parse_environ(args.container_environment.read_bytes())
        body, dropped = task_action_config(container, args.language)
        args.actions_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(args.actions_file, body)
        summary = {"reason": "written", "language": args.language, "dropped": dropped}
        print(json.dumps({**summary, "actions": str(args.actions_file)}))
        return
    if args.relay_port is None or args.output_dir is None:
        parser.error("需要--relay-port与--output-dir，或--container-environment与--actions-file")
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(args.output_dir / "product.json", product_config(args.relay_port))
    _write(args.output_dir / "actions.json", action_config(args.language))
    print(json.dumps({"reason": "written", "directory": str(args.output_dir)}))


if __name__ == "__main__":
    main()
