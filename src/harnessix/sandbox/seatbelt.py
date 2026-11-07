"""原生进程沙箱（ADR 0111）：把沙箱策略翻译成Seatbelt策略文本与`-D`参数。

策略文本只由本模块生成的固定片段与参数名组成；所有路径都经`-D`参数传入，不进入策略文本，
因此路径中的任何字符都不能改变策略结构。端口是经合同校验的整数，直接写入规则。
SBPL以最后一条匹配的规则为准（2026-11-07在macOS 27实测：同一端口先拒绝后放行即被放行），
所以全部放行规则在前、全部拒绝规则在后，任何后续放行都不能覆盖拒绝。
"""

from __future__ import annotations

from collections.abc import Sequence

from harnessix.agent.errors import KernelError
from harnessix.sandbox.native_contracts import NativeSandboxPolicy

SEATBELT_EXECUTABLE = "/usr/bin/sandbox-exec"
PROCESS_ARGV_LIMIT = 128
# 名字解析、用户信息、偏好、日志与临时目录查询所需的最小Mach服务；
# 不含LaunchServices、钥匙串（SecurityServer）、剪贴板与AppleEvents（ADR 0111第2.5节）。
_MACH_SERVICES = (
    "com.apple.bsd.dirhelper",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.diagnosticd",
    "com.apple.logd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
)
_BASE_RULES = (
    "(version 1)",
    "(deny default)",
    "(allow process-exec)",
    "(allow process-fork)",
    "(allow signal (target same-sandbox))",
    "(allow process-info* (target same-sandbox))",
    "(allow sysctl-read)",
    "(allow user-preference-read)",
    "(allow ipc-posix-sem)",
    "(allow ipc-posix-shm)",
    "(allow pseudo-tty)",
    "(allow mach-lookup " + " ".join(f'(global-name "{name}")' for name in _MACH_SERVICES) + ")",
    # 整机可读；拒读清单在后面覆盖。
    "(allow file-read*)",
    "(allow file-write* file-ioctl"
    ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty")'
    ' (literal "/dev/dtracehelper") (literal "/dev/ptmx")'
    ' (regex #"^/dev/fd/") (regex #"^/dev/ttys[0-9]+$"))',
)
# 与读工具一致的`.env`/`.env.*`（大小写不敏感），只对普通文件生效：`.env`虚拟环境目录不受影响。
_ENV_FILE_RULE = (
    "(deny file-read* file-write* (require-all"
    ' (regex #"/\\.[Ee][Nn][Vv](\\.[^/]*)?$") (vnode-type REGULAR-FILE)))'
)


def seatbelt_policy(policy: NativeSandboxPolicy) -> tuple[str, tuple[str, ...]]:
    """返回（策略文本，`-D`参数）；参数名按类别编号，值为策略中的路径。"""

    if policy.backend != "seatbelt":
        raise KernelError("sandbox_backend_mismatch", "策略不属于Seatbelt后端")
    rules = list(_BASE_RULES)
    parameters: list[str] = []

    def bind(prefix: str, values: Sequence[str]) -> str:
        names = []
        for index, value in enumerate(values):
            name = f"{prefix}_{index}"
            parameters.append(f"-D{name}={value}")
            names.append(f'(subpath (param "{name}"))')
        return " ".join(names)

    # 放行：可写目录；宿主回环（用户决策）；私有TMPDIR内的Unix套接字。
    rules.append(f"(allow file-write* {bind('WRITABLE', policy.writable_roots)})")
    parameters.append(f"-DPRIVATE_TMP={policy.private_tmp}")
    rules += [
        '(allow network-bind (local ip "localhost:*"))',
        '(allow network-inbound (local ip "localhost:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
        '(allow network-bind network-inbound (local unix-socket (subpath (param "PRIVATE_TMP"))))',
        '(allow network-outbound (remote unix-socket (subpath (param "PRIVATE_TMP"))))',
    ]
    # 拒绝：一律在最后。
    if policy.read_only_roots:
        rules.append(f"(deny file-write* {bind('READ_ONLY', policy.read_only_roots)})")
    rules.append(f"(deny file-read* file-write* {bind('DENIED', policy.denied_paths)})")
    if policy.deny_workspace_env_files:
        rules.append(_ENV_FILE_RULE)
    rules += [
        f'(deny network-outbound (remote ip "localhost:{port}"))'
        for port in policy.blocked_loopback_ports
    ]
    return "\n".join(rules) + "\n", tuple(parameters)


def seatbelt_argv(
    policy: NativeSandboxPolicy, *, command: Sequence[str], executable: str = SEATBELT_EXECUTABLE
) -> tuple[str, ...]:
    """完整argv；超出ProcessSpec项数上限时失败关闭。"""

    text, parameters = seatbelt_policy(policy)
    argv = (executable, "-p", text, *parameters, *command)
    if len(argv) > PROCESS_ARGV_LIMIT:
        raise KernelError("sandbox_policy_too_large", "Seatbelt参数超出进程参数上限")
    return argv
