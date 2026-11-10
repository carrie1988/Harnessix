"""零配置的命令工具（ADR 0111第2.2节、ADR 0113第2.3节）。

用户直接运行`harnessix code`或`harnessix exec`、没有指定Action配置时，按本机能力自动生成：
- 本机沙箱可用：沙箱内命令`exec_command.shell`（自动执行）与沙箱外命令`host_command.host-shell`
  （逐条批准）；
- 本机沙箱不可用：只有沙箱外命令，说明原因，每条都需批准；
- Windows：没有本机沙箱，只有沙箱外的PowerShell，每条都需批准（2026-11-10用户决定W2）。
只由用户入口（未指定Action配置时）显式请求；程序化与评测启动保持原内建配置。
用户可设置`HARNESSIX_ZERO_CONFIG_ACTIONS=0`关闭。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from harnessix.sandbox.native_contracts import HostSandboxProbeV2
from harnessix.sandbox.native_probe import probe_native_sandbox

from .action_contracts import (
    ProductActionConfigV1,
    ProductNativeProcessProfile,
    build_product_action_config,
    build_product_native_process_profile,
)
from .native_shells import POWERSHELL_ARGUMENTS, windows_powershell

ZERO_CONFIG_ENVIRONMENT = "HARNESSIX_ZERO_CONFIG_ACTIONS"
ZERO_CONFIG_VERSION = "zero-config/1"
SANDBOX_PROFILE_ID = "shell"
HOST_PROFILE_ID = "host-shell"
_SUPPORTED_SHELLS = frozenset({"zsh", "bash"})


def zero_config_wanted(action_config: object | None, environ: Mapping[str, str]) -> bool:
    """用户入口未指定Action配置、且没有显式关闭时请求零配置。"""

    return action_config is None and environ.get(ZERO_CONFIG_ENVIRONMENT) != "0"


def zero_config_shell(environ: Mapping[str, str]) -> str:
    """登录shell是zsh或bash时用它（命令语法与终端一致），否则用/bin/sh。"""

    shell = environ.get("SHELL", "")
    path = Path(shell)
    if (
        path.is_absolute()
        and path.name in _SUPPORTED_SHELLS
        and path.is_file()
        and os.access(path, os.X_OK)
    ):
        return shell
    return "/bin/sh"


def zero_config_action_config(
    environ: Mapping[str, str],
    *,
    probe: Callable[[], HostSandboxProbeV2] = probe_native_sandbox,
    platform: str = os.name,
) -> ProductActionConfigV1:
    """按本机沙箱探测结果生成命令工具；Windows只生成沙箱外PowerShell。"""

    if platform == "nt":
        return _windows_action_config(environ)
    if platform != "posix":
        return build_product_action_config()
    result = probe()
    shell = zero_config_shell(environ)
    profiles: list[ProductNativeProcessProfile] = []
    if result.available and result.backend is not None:
        profiles.append(
            build_product_native_process_profile(
                profile_id=SANDBOX_PROFILE_ID,
                version=ZERO_CONFIG_VERSION,
                description="本机沙箱中的Shell",
                backend=result.backend,
                program=shell,
                arguments=("-c",),
            )
        )
        host_description = "沙箱外的Shell，仅在沙箱内命令无法完成时使用"
    else:
        host_description = f"本机沙箱不可用（{result.reason_code}），所有命令都在沙箱外执行"
    profiles.append(
        build_product_native_process_profile(
            profile_id=HOST_PROFILE_ID,
            version=ZERO_CONFIG_VERSION,
            description=host_description,
            backend="host",
            program=shell,
            arguments=("-c",),
        )
    )
    return build_product_action_config(
        native_process_profiles=tuple(sorted(profiles, key=lambda item: item.profile_id))
    )


def _windows_action_config(environ: Mapping[str, str]) -> ProductActionConfigV1:
    program = windows_powershell(environ)
    if program is None:
        return build_product_action_config()
    profile = build_product_native_process_profile(
        profile_id=HOST_PROFILE_ID,
        version=ZERO_CONFIG_VERSION,
        description=(
            "沙箱外的Windows PowerShell，命令用PowerShell语法；Windows没有本机沙箱，每条都需批准"
        ),
        backend="host",
        program=program,
        arguments=POWERSHELL_ARGUMENTS,
    )
    return build_product_action_config(native_process_profiles=(profile,))
