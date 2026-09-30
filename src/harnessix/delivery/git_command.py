"""Git 固定命令材料：同步领域与异步产品端共用环境，不授予执行权限。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_identity import _executable_identity, _identity
from harnessix.execution.contracts import canonical_digest


@dataclass(frozen=True, slots=True)
class GitCommand:
    """受信宿主生成的不可变材料；正文不进入 repr，摘要仍覆盖全部输入。"""

    cwd: Path = field(repr=False)
    cwd_identity: str
    arguments: tuple[str, ...] = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    executable_identity: str
    input_data: bytes | None = field(repr=False)
    index_file: Path | None = field(repr=False)
    accepted: tuple[int, ...]
    timeout_seconds: float
    allowed_protocols: tuple[str, ...]

    @property
    def digest(self) -> str:
        """绑定 stdin 的有无、完整长度和摘要，避免相同 argv 授权不同正文。"""
        return canonical_digest(
            {
                "version": "git-fixed-command/v1",
                "cwd": os.path.normcase(str(self.cwd)),
                "cwd_identity": self.cwd_identity,
                "executable_identity": self.executable_identity,
                "argv": self.argv,
                "environment": self.environment,
                "input_present": self.input_data is not None,
                "input_bytes": len(self.input_data) if self.input_data is not None else 0,
                "input_sha256": hashlib.sha256(self.input_data or b"").hexdigest(),
                "accepted": self.accepted,
                "timeout_seconds": self.timeout_seconds,
                "allowed_protocols": self.allowed_protocols,
            }
        )


def fixed_git_environment(
    executable: Path,
    home: Path,
    temporary: Path,
    allowed_protocols: tuple[str, ...],
    index_file: Path | None,
) -> dict[str, str]:
    """复用原 Git 白名单环境；不继承用户凭据、代理、配置或模型环境。"""
    if (
        not allowed_protocols
        or allowed_protocols != tuple(sorted(set(allowed_protocols)))
        or any(value not in {"file", "https", "ssh"} for value in allowed_protocols)
    ):
        raise KernelError("git_protocol_invalid", "Git协议白名单无效")
    environment = {
        "PATH": os.pathsep.join((str(executable.parent), os.defpath)),
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": ":".join(allowed_protocols),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
    }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment.update(
            {
                "SystemRoot": system_root,
                "WINDIR": system_root,
                "COMSPEC": str(Path(system_root) / "System32/cmd.exe"),
            }
        )
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    return environment


@dataclass(frozen=True, slots=True)
class GitExecutionBinding:
    """固定环境的唯一构造职责；Runner 和产品端不能各自拼装一套命令。"""

    executable: Path
    identity: str
    home: Path
    temporary: Path
    global_arguments: tuple[str, ...]

    def prepare(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        input_data: bytes | None = None,
        index_file: Path | None = None,
        accepted: tuple[int, ...] = (0,),
        timeout: float = 20.0,
        allowed_protocols: tuple[str, ...] = ("file",),
    ) -> GitCommand:
        """观察物理身份并生成不可变材料，不启动进程。"""
        if _executable_identity(self.executable) != self.identity:
            raise KernelError("git_executable_changed", "Git可执行文件身份已经变化")
        if index_file is not None and (
            not index_file.is_absolute() or index_file.parent != self.temporary
        ):
            raise KernelError("git_index_invalid", "Git临时索引不属于私有目录")
        environment = fixed_git_environment(
            self.executable, self.home, self.temporary, allowed_protocols, index_file
        )
        return GitCommand(
            cwd=cwd,
            cwd_identity=_identity(cwd, directory=True),
            arguments=arguments,
            argv=(str(self.executable), *self.global_arguments, *arguments),
            environment=tuple(sorted(environment.items())),
            executable_identity=self.identity,
            input_data=input_data,
            index_file=index_file,
            accepted=accepted,
            timeout_seconds=float(timeout),
            allowed_protocols=allowed_protocols,
        )

    def verify(self, command: GitCommand) -> None:
        """从固定宿主材料重建并比较，拒绝目录、程序、环境或参数替换。"""
        current = self.prepare(
            command.cwd,
            command.arguments,
            input_data=command.input_data,
            index_file=command.index_file,
            accepted=command.accepted,
            timeout=command.timeout_seconds,
            allowed_protocols=command.allowed_protocols,
        )
        if current != command:
            raise KernelError("git_command_binding_changed", "Git固定命令绑定已经变化")
