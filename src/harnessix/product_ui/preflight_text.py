"""Doctor与启动界面的可读文字。

预检报告只含稳定标识（docs/operations/diagnostics.md），文字在渲染时查表，不进入报告合同。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from harnessix.product_config.preflight_actions import NATIVE_SANDBOX_CHECK_ID
from harnessix.product_config.product_contracts import ProductPreflightReport

_REMEDIATION_TEXT: Final[Mapping[str, str]] = {
    # 配置与Profile
    "run_configure": "运行 harnessix code configure 生成配置文件",
    "migrate_or_configure": "配置文件内容无效或版本过旧；重新运行 harnessix code configure",
    "select_profile": "用 --profile 选择配置文件里存在的Profile",
    "install_provider_extra": (
        "安装所选模型服务的依赖，例如 pip install 'harnessix[openai]' 或 'harnessix[anthropic]'"
    ),
    "set_environment_secret": "在环境变量里设置配置文件声明的密钥后重新启动",
    # 运行环境
    "use_supported_platform": "该能力只支持macOS与Linux",
    "install_tui_extra": "安装终端界面依赖：pip install 'harnessix[tui]'",
    "inspect_workspace": "确认工作区目录存在且可读写，或在命令后指定其他工作区目录",
    "choose_state_directory": "用 --state-directory 指定一个可写的状态目录",
    "omit_or_install_git": "安装Git，或用 --git-executable 指定git程序的绝对路径",
    # Action配置与能力
    "fix_action_config": "检查 --action-config 指定的文件能否读取、格式是否正确",
    "inspect_action_capabilities": "先修复上面失败的配置检查，再重新运行 harnessix code doctor",
    "inspect_action_capability": "运行 harnessix code doctor --json 查看该能力的原因码",
    "enable_action_capability": "该能力在Action配置里被关闭；需要时在配置中开启",
    "install_supported_container_engine": "安装受支持的容器引擎（Docker）",
    "start_container_engine": "启动容器引擎（Docker）后重新运行",
    "install_pinned_container_image": "拉取Action配置里固定版本的容器镜像",
    "fix_action_profile_limits": "本机无法执行Profile声明的资源限制；调低或删除这些限制",
    "repair_process_owner": (
        "本机无法启动受管子进程；确认在普通用户终端里运行，且Python可创建子进程"
    ),
    "fix_action_secret_target": "Action配置里的密钥注入目标无效；按文档修正",
    "update_action_secret_version": "密钥版本已变化；更新Action配置里的密钥版本",
    "inspect_action_profile": "Profile自检失败；运行 harnessix code doctor --json 查看原因码",
    # 本机沙箱（ADR 0111、ADR 0113）
    "specify_workspace": "在命令后指定工作区目录，沙箱需要它确定可写范围",
    "run_outside_sandboxed_parent": (
        "Harnessix运行在另一个macOS沙箱里（例如其他工具提供的沙箱终端），macOS不允许嵌套；"
        "请在系统自带的“终端”里直接运行"
    ),
    "inspect_seatbelt_policy": (
        "macOS拒绝了沙箱规则；请附上 harnessix code doctor --json 的输出反馈问题"
    ),
    "install_bubblewrap": (
        "安装bubblewrap（Debian/Ubuntu：sudo apt install bubblewrap；"
        "Fedora：sudo dnf install bubblewrap），然后重新启动"
    ),
    "enable_unprivileged_user_namespaces": (
        "系统禁止普通用户创建隔离环境；请管理员允许非特权用户命名空间"
        "（如 sysctl kernel.unprivileged_userns_clone=1，"
        "Ubuntu 24.04起还需为bwrap配置AppArmor例外）；"
        "在容器里运行时改到宿主机上运行"
    ),
    "inspect_bubblewrap": "bubblewrap无法运行测试命令；运行 bwrap --ro-bind / / /bin/true 查看报错",
    "move_workspace_out_of_protected_paths": (
        "工作区与受保护目录（如 ~/.ssh、~/.aws）重叠；把项目移到普通目录"
    ),
    "reduce_sandbox_write_roots": "沙箱规则过长；减少Profile的write_roots或工作区里的.env文件",
    "fix_sandbox_paths": "Profile里的路径必须是存在的绝对路径",
}

_SANDBOX_BACKEND_NAMES: Final[Mapping[str, str]] = {
    "native_sandbox_seatbelt": "macOS Seatbelt",
    "native_sandbox_bubblewrap": "bubblewrap",
}


def remediation_text(remediation_id: str) -> str | None:
    """修复标识对应的可照做说明；未登记的标识返回None，调用方只显示标识。"""

    return _REMEDIATION_TEXT.get(remediation_id)


def sandbox_status_line(report: ProductPreflightReport) -> str | None:
    """零配置时的一行沙箱状态；未请求零配置（如显式Action配置）时返回None。"""

    check = next((item for item in report.checks if item.check_id == NATIVE_SANDBOX_CHECK_ID), None)
    if check is None:
        return None
    if check.status == "passed":
        return f"沙箱：已启用（{_SANDBOX_BACKEND_NAMES.get(check.code, check.code)}）"
    return "沙箱：不可用，命令逐条审批（运行 harnessix code doctor 查看原因）"
