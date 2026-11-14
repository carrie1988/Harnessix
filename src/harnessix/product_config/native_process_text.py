"""原生命令工具说明里的边界与检查命令写法（与native_process的执行逻辑分开）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .action_contracts import ProductNativeProcessProfile

# Shell以最后一条语句的结果作为整条命令的退出码：`pytest; echo done`、PowerShell里测试之后再
# Write-Host，失败的检查都会记成成功，完成证据随之写成“修改后检查通过”（Beta Issue #27）。
CHECK_COMMAND_GUIDANCE: Final = (
    "作为检查运行的测试或构建命令，要以检查本身结尾，或用exit $?（PowerShell用exit $LASTEXITCODE）"
    "带出它的退出码；之后再打印、过滤输出或接其他命令，失败会被记成成功。"
)


def boundary_description(profile: ProductNativeProcessProfile) -> str:
    """如实写明边界（含TMPDIR每条命令前清空），模型不必试错；外部边界文本与指纹不变。"""

    if profile.backend == "external_boundary":
        return (
            "命令从工作区根目录开始，可直接读写工作区，改动会保留；"
            "Harnessix不另加沙箱，隔离与网络由外部环境决定。"
        )
    if profile.backend == "host":
        return (
            "命令在沙箱外从工作区根目录开始运行：没有隔离，可以联网，可以读写用户目录；"
            "每条命令都需要用户批准，reason会展示给用户。只在沙箱内的命令因需要联网"
            "（如安装依赖、下载）或写工作区以外位置而无法完成时使用；其余命令请用沙箱内的命令工具。"
        )
    workspace = (
        "可直接读写工作区，改动会保留" if profile.workspace_mode == "read_write" else "工作区只读"
    )
    return (
        f"命令在本机沙箱中从工作区根目录开始运行，{workspace}；"
        "可读整台机器，但凭据、命令历史、Harnessix状态与工作区.env文件不可读；"
        "只能写工作区、TMPDIR与私有缓存目录，HOME不可写；"
        "TMPDIR在每条命令开始前清空，后面的命令还要用的文件（如编译产物、测试夹具）请放在工作区里；"
        ".git只读，提交与暂存由Harnessix交付流程完成；没有网络，联网操作会失败。"
    )
