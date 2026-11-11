"""启动失败时给用户看的说明：先说为什么不能启动，再给能直接照做的步骤；完整检查清单留给doctor。

此前启动失败直接打印整张预检清单（十几行“[通过] category/check_id: code”），真正要做的事夹在
一行英文原因码里，用户不知道怎么办（2026-11-10 macOS实测：缺少API Key）。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot
from harnessix.product_config.product_contracts import ProductPreflightCheck, ProductPreflightReport
from harnessix.product_ui.preflight_text import remediation_text

_TITLES: Final = {
    "run_configure": "还没有配置模型",
    "migrate_or_configure": "配置文件无效或版本过旧",
    "select_profile": "找不到指定的模型配置（Profile）",
    "install_provider_extra": "缺少模型服务的依赖组件",
    "set_environment_secret": "缺少模型的 API Key",
    "use_long_self_hosted_key": "本地模型的 Key 太短",
    "install_tui_extra": "缺少终端界面组件",
    "inspect_workspace": "工作区目录不可用",
    "choose_state_directory": "状态目录不可写",
    "run_outside_sandboxed_parent": "Harnessix 运行在另一个沙箱里",
}
_DOCTOR_HINT: Final = "需要排查时运行 harnessix code doctor 查看完整检查结果。"


def startup_failure_text(report: ProductPreflightReport, config: Path | None) -> str:
    """不可启动时的说明；只列阻止启动的项，每项给出标题与可照做的步骤。"""

    blocking = [c for c in report.checks if c.status == "failed" and c.requirement == "required"]
    failed = blocking or [c for c in report.checks if c.status == "failed"]
    if not failed:
        return "Harnessix 无法启动。\n\n" + _DOCTOR_HINT
    if len(failed) == 1:
        check = failed[0]
        first, *rest = _steps(check, config).splitlines()
        return "\n".join(
            [
                f"Harnessix 无法启动：{_title(check)}。",
                "",
                "请这样做：",
                f"  1. {first}",
                *(f"     {line}" for line in rest),
                "  2. 重新运行刚才的命令。",
                "",
                _DOCTOR_HINT,
            ]
        )
    lines = [f"Harnessix 无法启动，有 {len(failed)} 个问题需要处理：", ""]
    for index, check in enumerate(failed, start=1):
        lines.append(f"{index}. {_title(check)}")
        lines.append(_indent(_steps(check, config), "   "))
    lines += ["", "处理后重新运行刚才的命令。", "", _DOCTOR_HINT]
    return "\n".join(lines)


def _title(check: ProductPreflightCheck) -> str:
    title = _TITLES.get(check.remediation_id or "")
    return title if title is not None else f"启动检查未通过（{check.code}）"


def _steps(check: ProductPreflightCheck, config: Path | None) -> str:
    if check.remediation_id == "set_environment_secret":
        return _secret_steps(_secret_variables(config))
    text = remediation_text(check.remediation_id) if check.remediation_id else None
    return text or "运行 harnessix code doctor 查看具体原因"


def _secret_steps(names: Sequence[str]) -> str:
    """按平台给出设置环境变量的命令；变量名来自配置文件，本身不是机密。"""

    variables = names or ("MODEL_API_KEY",)
    if os.name == "nt":
        commands = [f'$env:{name}="你的API Key"' for name in variables]
        persist = f'想以后打开终端都生效，改用 setx {variables[0]} "你的API Key"，再打开新终端。'
    else:
        commands = [f"export {name}=你的API Key" for name in variables]
        persist = (
            "想以后打开终端都生效，把上面这一行加到 ~/.zshrc（zsh）或 ~/.bash_profile（bash）末尾。"
        )
    return "在当前终端设置 API Key：\n" + "\n".join("  " + c for c in commands) + "\n" + persist


def _secret_variables(config: Path | None) -> tuple[str, ...]:
    if config is None:
        return ()
    try:
        snapshot = load_product_config(config)
    except Exception:
        return ()
    if not isinstance(snapshot, ProductConfigSnapshot):
        return ()
    names = (
        getattr(source, "environment_variable", None) for source in snapshot.config.secret_sources
    )
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
