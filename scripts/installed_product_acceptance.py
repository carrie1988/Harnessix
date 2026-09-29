"""从实际Wheel安装环境验证产品状态恢复及卸载重装，不发送模型Turn。

仅允许独立环境根内新建的case目录；产品State始终由正式入口创建，
不预建私有Root、不改既有ACL、不导出数据库、Key或业务正文。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4
from zipfile import ZipFile

import harnessix
from harnessix.product_config.state_restore_contracts import ProductStateRestoreResult
from harnessix.sdk.agent_client import AgentClient
from harnessix.sdk.subprocess import SubprocessAgentTransport


class AcceptanceFailure(RuntimeError):
    """固定验收错误码；公开结果不展开子进程输出或私有异常。"""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceFailure(code)


def check_environment(
    root: Path, source: Path, *, prefix: Path, isolated: bool, paths: list[str]
) -> None:
    """证明执行环境不借用源码，且不能将用户已有环境误作卸载目标。"""

    root, source, prefix = root.resolve(), source.resolve(), prefix.resolve()
    require(
        isolated
        and prefix == root / "venv"
        and not root.is_relative_to(source)
        and not source.is_relative_to(root)
        and not any(Path(item).resolve().is_relative_to(source) for item in paths),
        "installed_environment_invalid",
    )


def check_package_members(wheel: Path, package: Path) -> int:
    """逐成员比较实际安装包与Wheel；不将路径穿越成员当作正式输入。"""

    names: set[str] = set()
    with ZipFile(wheel) as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith("harnessix/"):
                continue
            relative = PurePosixPath(member.filename).relative_to("harnessix")
            require(
                bool(relative.parts)
                and ".." not in relative.parts
                and "\\" not in member.filename
                and member.filename not in names,
                "installed_package_mismatch",
            )
            path = package.joinpath(*relative.parts)
            require(
                path.is_file()
                and not path.is_symlink()
                and path.read_bytes() == archive.read(member),
                "installed_package_mismatch",
            )
            names.add(member.filename)
    require(bool(names), "installed_package_mismatch")
    return len(names)


def state_snapshot(case: Path) -> dict[str, str]:
    """关闭所有Writer后仅在内存比较自有夹具文件，不发布Key摘要或正文。"""

    result: dict[str, str] = {}
    for path in sorted(case.rglob("*")):
        require(
            not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(),
            "acceptance_fixture_unsafe",
        )
        if path.is_file():
            result[path.relative_to(case).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def cli(arguments: tuple[str, ...], *, expected_exit: int = 0) -> dict:
    """在同一已安装解释器中执行正式CLI；非预期失败不公开stdout/stderr。"""

    result = subprocess.run(
        (sys.executable, "-I", "-m", "harnessix", *arguments),
        capture_output=True,
        timeout=60,
    )
    require(result.returncode == expected_exit, "installed_cli_failed")
    value = json.loads(result.stdout if expected_exit == 0 else result.stderr)
    require(isinstance(value, dict), "installed_cli_result_invalid")
    return value


@dataclass(frozen=True)
class InstalledCase:
    """独立Git Workspace、源码外配置及产品地址；State不由验收器创建。"""

    directory: Path
    workspace: Path
    config: Path
    state: Path
    backup: Path

    @property
    def server_command(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-I",
            "-m",
            "harnessix",
            "agent-server",
            "--config",
            str(self.config),
            "--workspace",
            str(self.workspace),
            "--state-directory",
            str(self.state),
        )

    @property
    def state_arguments(self) -> tuple[str, ...]:
        return ("--state-directory", str(self.state), "--backup-directory", str(self.backup))


def prepare_case(root: Path) -> InstalledCase:
    directory = root / "case"
    # exist_ok保持False：失败夹具与存量状态不自动清理、不覆盖后重试。
    directory.mkdir(mode=0o700)
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    (workspace / "preserved.txt").write_bytes(b"unchanged workspace\n")
    case = InstalledCase(
        directory,
        workspace,
        directory / "private/config.json",
        directory / "state",
        directory / "backup",
    )
    os.environ["HARNESSIX_INSTALL_FIXTURE_KEY"] = "install-fixture-not-a-real-key"
    cli(
        (
            "code",
            "configure",
            "--config",
            str(case.config),
            "--provider-kind",
            "openai_chat",
            "--base-url",
            "https://provider.invalid/v1",
            "--model",
            "installation-fixture",
            "--api-key-env",
            "HARNESSIX_INSTALL_FIXTURE_KEY",
            "--output-token-parameter",
            "max_tokens",
            "--non-interactive",
        )
    )
    doctor = cli(
        (
            "code",
            "doctor",
            str(workspace),
            "--config",
            str(case.config),
            "--state-directory",
            str(case.state),
            "--json",
        )
    )
    require(doctor["ready"] is True and not case.state.exists(), "installed_doctor_failed")
    return case


async def session(case: InstalledCase, create: str | None = None) -> tuple[UUID | None, set[UUID]]:
    transport = SubprocessAgentTransport(case.server_command)
    client = AgentClient(transport)
    try:
        async with asyncio.timeout(30):
            initialized = await client.initialize()
            require(
                initialized.server_info.version == version("harnessix"),
                "installed_server_version_mismatch",
            )
            created = (
                None
                if create is None
                else await client.create_thread(str(case.workspace), request_id=create)
            )
            page = await client.list_threads(limit=50)
        require(page.next_cursor is None, "installed_thread_page_invalid")
        return None if created is None else created.thread_id, {x.thread_id for x in page.threads}
    finally:
        await client.close()
        require(transport.snapshot().state == "closed", "installed_transport_not_closed")


async def create_with_backup_refusal(case: InstalledCase) -> UUID:
    """通过正式产品创建首个Thread，并证明活跃Owner拒绝停机备份。"""
    transport = SubprocessAgentTransport(case.server_command)
    client = AgentClient(transport)
    try:
        async with asyncio.timeout(30):
            initialized = await client.initialize()
            require(
                initialized.server_info.version == version("harnessix"),
                "installed_server_version_mismatch",
            )
            first = await client.create_thread(str(case.workspace), request_id="before-backup")
        busy = await asyncio.to_thread(
            cli, ("state", "backup", *case.state_arguments), expected_exit=2
        )
        require(
            busy["code"] == "product_state_busy" and not case.backup.exists(),
            "installed_owner_refusal_invalid",
        )
    finally:
        await client.close()
        require(transport.snapshot().state == "closed", "installed_transport_not_closed")
    return first.thread_id


async def create_verified_backup(case: InstalledCase) -> dict:
    """停机后备份并验真原六库及Key，不用直接Store调用补造备份。"""
    created = await asyncio.to_thread(cli, ("state", "backup", *case.state_arguments))
    verified = await asyncio.to_thread(cli, ("state", "verify", *case.state_arguments))
    require(
        created["status"] == "backed_up"
        and created["files"] == 7
        and verified["status"] == "verified"
        and verified["backup_id"] == created["backup_id"],
        "installed_backup_invalid",
    )
    return created


async def verify_restore(case: InstalledCase) -> tuple[dict, set[UUID]]:
    """实际活跃互斥、原Key整体恢复和稳定ID；不用直接Store调用替代产品入口。"""
    first = await create_with_backup_refusal(case)
    key = case.state / "session-auth/key.v1"
    original_key = hashlib.sha256(key.read_bytes()).digest()
    created = await create_verified_backup(case)
    second, observed = await session(case, "after-backup")
    require(observed == {first, second}, "installed_thread_state_invalid")
    restore = (
        "state",
        "restore",
        *case.state_arguments,
        "--restore-id",
        str(uuid4()),
        "--confirm-backup",
        created["backup_id"],
    )
    restored = ProductStateRestoreResult.model_validate(await asyncio.to_thread(cli, restore))
    require(
        restored.status == "restored" and restored.retained_previous_state,
        "installed_restore_invalid",
    )
    third, observed = await session(case, "after-restore")
    require(observed == {first, third}, "installed_restored_threads_invalid")
    repeated = ProductStateRestoreResult.model_validate(await asyncio.to_thread(cli, restore))
    _, final = await session(case)
    require(
        repeated == restored
        and final == observed
        and hashlib.sha256(key.read_bytes()).digest() == original_key,
        "installed_restore_identity_invalid",
    )
    return {"backup_file_count": created["files"], "stable_restore_result": True}, final


def uninstall_reinstall(case: InstalledCase, uv: Path, wheel: Path, root: Path) -> None:
    """卸载仅针对专用venv；状态/Key/备份/Workspace逐文件原字节必须不变。"""

    before = state_snapshot(case.directory)
    subprocess.run(
        [str(uv), "pip", "uninstall", "--python", sys.executable, "harnessix"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.util; assert importlib.util.find_spec('harnessix') is None",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    require(state_snapshot(case.directory) == before, "installed_uninstall_changed_state")
    requirement = root / "reinstall-wheel.txt"
    requirement.write_text(
        f"{wheel.as_uri()} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(uv),
            "pip",
            "install",
            "--python",
            sys.executable,
            "--offline",
            "--no-deps",
            "--require-hashes",
            "-r",
            str(requirement),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    require(state_snapshot(case.directory) == before, "installed_reinstall_changed_state")


def prepare_environment(arguments: argparse.Namespace) -> tuple[Path, Path, Path, int, str]:
    """先在目录线程完成路径、安装字节及元数据检查，再启动异步产品客户端。"""

    root, source, wheel = (
        arguments.environment_root.resolve(),
        arguments.source_root.resolve(),
        arguments.wheel.resolve(),
    )
    check_environment(
        root, source, prefix=Path(sys.prefix), isolated=bool(sys.flags.isolated), paths=sys.path
    )
    package = Path(harnessix.__file__).resolve().parent
    require(package.is_relative_to(Path(sys.prefix).resolve()), "installed_environment_invalid")
    members = check_package_members(wheel, package)
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    require(revision == arguments.source_revision, "acceptance_source_revision_mismatch")
    require(
        check_package_members(wheel, source / "src/harnessix") == members,
        "acceptance_source_package_mismatch",
    )
    product_version = version("harnessix")
    return root, wheel, package, members, product_version


async def accept(arguments: argparse.Namespace) -> dict:
    root, wheel, package, members, product_version = await asyncio.to_thread(
        prepare_environment, arguments
    )
    print(json.dumps({"phase": "installed-package", "status": "passed"}), flush=True)
    case = await asyncio.to_thread(prepare_case, root)
    restored, expected_threads = await verify_restore(case)
    print(json.dumps({"phase": "full-state-restore", "status": "passed"}), flush=True)
    await asyncio.to_thread(uninstall_reinstall, case, arguments.uv.resolve(), wheel, root)
    require(
        await asyncio.to_thread(check_package_members, wheel, package) == members,
        "installed_package_mismatch",
    )
    _, observed = await session(case)
    require(
        observed == expected_threads
        and await asyncio.to_thread((case.workspace / "preserved.txt").read_bytes)
        == b"unchanged workspace\n",
        "installed_reinstall_read_failed",
    )
    return {
        "spec_version": "harnessix.installed-product-acceptance/v1",
        "source_revision": arguments.source_revision,
        "product_version": product_version,
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "wheel_sha256": hashlib.sha256(await asyncio.to_thread(wheel.read_bytes)).hexdigest(),
        "installed_package_members_verified": members,
        "source_package_members_verified": members,
        "isolated": True,
        "source_checkout_in_sys_path": False,
        "doctor_ready": True,
        "active_owner_backup_refused": True,
        "full_backup_verified": True,
        "original_key_retained": True,
        "post_snapshot_thread_removed": True,
        "prior_root_retained": True,
        "same_restore_id_does_not_rewind_new_state": True,
        **restored,
        "uninstalled_import_absent": True,
        "uninstall_preserves_all_fixture_bytes": True,
        "same_wheel_reinstall_verified": True,
        "reinstalled_threads_readable": True,
        "workspace_unchanged": True,
        "provider_turn_requests": 0,
        "commercial_release": False,
        "not_proven": ["real_coding_task", "version_upgrade", "beta", "consumer_os_support"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="源码外安装产品状态恢复及卸载重装验收")
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.environment_root / "result.json"
    try:
        require(
            bool(re.fullmatch(r"[0-9a-f]{40}", arguments.source_revision)),
            "acceptance_input_invalid",
        )
        result = asyncio.run(accept(arguments))
    except Exception as error:
        code = str(error) if isinstance(error, AcceptanceFailure) else "installed_acceptance_failed"
        print(json.dumps({"status": "failed", "code": code}), file=sys.stderr)
        return 1
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "commercial_release": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
