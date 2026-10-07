"""源码外不同版本停机升级与备份回退；每阶段使用新的已安装隔离解释器。

仅调度既有安装检查、正式SDK及状态CLI，不实现第二套生产恢复算法。
私有Thread/备份身份只经捕获管道传递，Key与文件快照仅在控制器内存核对。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile


def _load_helpers():
    """复用相邻验收模块，不把源码或scripts目录加入隔离解释器的sys.path。"""
    name = "_harnessix_installed_acceptance"
    path = Path(__file__).with_name("installed_product_acceptance.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("upgrade_helpers_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_helpers = _load_helpers()
AcceptanceFailure = _helpers.AcceptanceFailure
require = _helpers.require
_BASELINE_REVISION = "a4f7f33449bb897d84fe3a8e8262307943233fb4"
_BASELINE_SHA256 = "5a6e144487dd79679f609b709743db176ee28bdca077084ac8901efdc9f2fe30"


@dataclass(frozen=True)
class WheelIdentity:
    """来自完整原字节及唯一发行物METADATA的身份，不从文件名推断版本。"""

    version: str
    sha256: str


def read_wheel_identity(wheel: Path, expected_sha256: str) -> WheelIdentity:
    """安装前拒绝输入摘要漂移、重复版本和其他发行物，原Wheel始终只读。"""
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None
        and wheel.is_file()
        and not wheel.is_symlink()
        and hashlib.sha256(wheel.read_bytes()).hexdigest() == expected_sha256,
        "upgrade_wheel_identity_invalid",
    )
    with ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        require(len(names) == 1, "upgrade_wheel_metadata_invalid")
        value = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    versions = value.get_all("Version", [])
    require(
        value.get_all("Name") == ["harnessix"]
        and len(versions) == 1
        and bool(versions[0])
        and names == [f"harnessix-{versions[0]}.dist-info/METADATA"],
        "upgrade_wheel_metadata_invalid",
    )
    return WheelIdentity(versions[0], expected_sha256)


def check_upgrade_pair(baseline: WheelIdentity, candidate: WheelIdentity) -> None:
    """验收固定不同版本对；同版本重装和稳定发行版本不能借本专项宣称通过。"""
    require(baseline.version != candidate.version, "upgrade_versions_equal")
    require(
        (baseline.version, candidate.version) == ("0.1.0", "1.0.0rc1"),
        "upgrade_version_scope_invalid",
    )


def require_phase_threads(observed: set[str], expected: set[str]) -> None:
    """精确核对完整集合，升级或回退中缺失/多出的Thread均不得被忽略。"""
    require(observed == expected, "upgrade_thread_state_invalid")


def _environment(arguments: argparse.Namespace, wheel: Path, identity: WheelIdentity) -> int:
    root, source = arguments.environment_root.resolve(), arguments.source_root.resolve()
    _helpers.check_environment(
        root, source, prefix=Path(sys.prefix), isolated=bool(sys.flags.isolated), paths=sys.path
    )
    package = Path(_helpers.harnessix.__file__).resolve().parent
    require(package.is_relative_to(Path(sys.prefix).resolve()), "installed_environment_invalid")
    require(_helpers.version("harnessix") == identity.version, "installed_server_version_mismatch")
    members = _helpers.check_package_members(wheel, package)
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    require(revision == arguments.source_revision, "acceptance_source_revision_mismatch")
    for filename in ("installed_product_acceptance.py", "installed_product_upgrade_acceptance.py"):
        committed = subprocess.run(
            ["git", "-C", str(source), "show", f"{revision}:scripts/{filename}"],
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout
        require(
            Path(__file__).with_name(filename).read_bytes() == committed,
            "upgrade_script_revision_mismatch",
        )
    if identity.version == "1.0.0rc1":
        require(
            _helpers.check_package_members(wheel, source / "src/harnessix") == members,
            "acceptance_source_package_mismatch",
        )
    return members


def _case(root: Path):
    directory = root / "case"
    return _helpers.InstalledCase(
        directory,
        directory / "workspace",
        directory / "private/config.json",
        directory / "state",
        directory / "backup",
    )


async def _restore_phase(case, arguments: argparse.Namespace) -> set[UUID]:
    """候选先恢复升级前整组状态，再创建C；重复原恢复ID不得回退后续C。"""
    restore = (
        "state",
        "restore",
        *case.state_arguments,
        "--restore-id",
        arguments.restore_id,
        "--confirm-backup",
        arguments.backup_id,
    )
    restored = await asyncio.to_thread(_helpers.cli, restore)
    require(
        restored["status"] == "restored" and restored["retained_previous_state"],
        "installed_restore_invalid",
    )
    _, original = await _helpers.session(case)
    require_phase_threads({str(x) for x in original}, set(arguments.before_threads))
    third, observed = await _helpers.session(case, "upgrade-after-restore")
    require_phase_threads({str(x) for x in observed}, set(arguments.before_threads) | {str(third)})
    repeated = await asyncio.to_thread(_helpers.cli, restore)
    _, final = await _helpers.session(case)
    require(repeated == restored and final == observed, "installed_restore_identity_invalid")
    return final


async def _phase(arguments: argparse.Namespace) -> dict:
    """新隔离进程中的正式产品操作；返回私有夹具身份而不写阶段公开文件。"""
    identity = read_wheel_identity(arguments.wheel, arguments.wheel_sha256)
    members = await asyncio.to_thread(_environment, arguments, arguments.wheel, identity)
    os.environ["HARNESSIX_INSTALL_FIXTURE_KEY"] = "install-fixture-not-a-real-key"
    case = _case(arguments.environment_root)
    backup_id = arguments.backup_id
    if arguments.phase == "baseline":
        case = await asyncio.to_thread(_helpers.prepare_case, arguments.environment_root)
        first = await _helpers.create_with_backup_refusal(case)
        backup = await _helpers.create_verified_backup(case)
        observed, backup_id = {first}, backup["backup_id"]
    elif arguments.phase == "restore":
        observed = await _restore_phase(case, arguments)
    else:
        _, before = await _helpers.session(case)
        require_phase_threads({str(x) for x in before}, set(arguments.before_threads))
        created, observed = await _helpers.session(case, f"upgrade-{arguments.phase}")
        require_phase_threads(
            {str(x) for x in observed}, set(arguments.before_threads) | {str(created)}
        )
    return {
        "phase": arguments.phase,
        "version": identity.version,
        "wheel_sha256": identity.sha256,
        "installed_members": members,
        "threads": sorted(str(x) for x in observed),
        "backup_id": backup_id,
    }


def run_phase(
    arguments: argparse.Namespace,
    wheel: Path,
    digest: str,
    phase: str,
    before: tuple[str, ...] = (),
    backup_id: str = "",
    restore_id: str = "",
) -> dict:
    """每阶段重开解释器；捕获私有结果，失败保留case且不展开第三方异常。"""
    command = (
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--environment-root",
        str(arguments.environment_root),
        "--source-root",
        str(arguments.source_root),
        "--source-revision",
        arguments.source_revision,
        "--wheel",
        str(wheel),
        "--wheel-sha256",
        digest,
        "--uv",
        str(arguments.uv),
        "--phase",
        phase,
        "--backup-id",
        backup_id,
        "--restore-id",
        restore_id,
        "--before-threads",
        *before,
    )
    try:
        result = subprocess.run(command, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired as error:
        raise AcceptanceFailure("upgrade_phase_failed") from error
    require(result.returncode == 0, "upgrade_phase_failed")
    value = json.loads(result.stdout)
    require(type(value) is dict and value.get("phase") == phase, "upgrade_phase_failed")
    return value


def _install(arguments: argparse.Namespace, wheel: Path, digest: str, phase: str) -> None:
    requirement = arguments.environment_root / f"{phase}-wheel.txt"
    with requirement.open("x", encoding="utf-8") as output:
        output.write(f"{wheel.resolve().as_uri()} --hash=sha256:{digest}\n")
    result = subprocess.run(
        [
            str(arguments.uv),
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
        capture_output=True,
        timeout=120,
    )
    require(result.returncode == 0, "upgrade_install_failed")


def _switch_without_state_change(
    arguments: argparse.Namespace, wheel: Path, digest: str, phase: str
) -> None:
    case = _case(arguments.environment_root)
    before = _helpers.state_snapshot(case.directory)
    _install(arguments, wheel, digest, phase)
    require(_helpers.state_snapshot(case.directory) == before, "upgrade_install_changed_state")


def accept_upgrade(arguments: argparse.Namespace) -> dict:
    """仅调度既有产品和指定venv的停机切换，原Key及快照只在内存中核对。"""
    baseline = read_wheel_identity(arguments.baseline_wheel, arguments.baseline_sha256)
    candidate = read_wheel_identity(arguments.wheel, arguments.wheel_sha256)
    check_upgrade_pair(baseline, candidate)
    require(
        arguments.baseline_source_revision == _BASELINE_REVISION
        and baseline.sha256 == _BASELINE_SHA256,
        "upgrade_baseline_source_invalid",
    )
    members = _environment(arguments, arguments.wheel, candidate)
    require(not (arguments.environment_root / "case").exists(), "upgrade_case_exists")
    _install(arguments, arguments.baseline_wheel, baseline.sha256, "baseline")
    first = run_phase(arguments, arguments.baseline_wheel, baseline.sha256, "baseline")
    case = _case(arguments.environment_root)
    key_path = case.state / "session-auth/key.v1"
    original_key = key_path.read_bytes()
    _switch_without_state_change(arguments, arguments.wheel, candidate.sha256, "candidate")
    second = run_phase(
        arguments, arguments.wheel, candidate.sha256, "upgraded", tuple(first["threads"])
    )
    require(second["version"] == candidate.version, "installed_server_version_mismatch")
    restore_id = str(uuid4())
    restored = run_phase(
        arguments,
        arguments.wheel,
        candidate.sha256,
        "restore",
        tuple(first["threads"]),
        first["backup_id"],
        restore_id,
    )
    require(key_path.read_bytes() == original_key, "installed_restore_identity_invalid")
    # 当前Runtime验收会再次升代；旧版切换前必须重建与旧版匹配的完整备份。
    rollback_restore_id = str(uuid4())
    require(rollback_restore_id != restore_id, "upgrade_restore_identity_reused")
    rollback_state = _helpers.cli(
        (
            "state",
            "restore",
            *case.state_arguments,
            "--restore-id",
            rollback_restore_id,
            "--confirm-backup",
            first["backup_id"],
        )
    )
    require(
        rollback_state["status"] == "restored" and rollback_state["retained_previous_state"],
        "installed_restore_invalid",
    )
    require(key_path.read_bytes() == original_key, "installed_restore_identity_invalid")
    # 此处只切换发行物；不得再启动会前向升代的当前Runtime。
    _switch_without_state_change(arguments, arguments.baseline_wheel, baseline.sha256, "rollback")
    final = run_phase(
        arguments, arguments.baseline_wheel, baseline.sha256, "rollback", tuple(first["threads"])
    )
    require(
        final["version"] == baseline.version and key_path.read_bytes() == original_key,
        "installed_restore_identity_invalid",
    )
    require(
        (case.workspace / "preserved.txt").read_bytes() == b"unchanged workspace\n",
        "installed_reinstall_read_failed",
    )
    return {
        "spec_version": "harnessix.installed-product-upgrade-acceptance/v1",
        "source_revision": arguments.source_revision,
        "baseline_source_revision": arguments.baseline_source_revision,
        "baseline_version": baseline.version,
        "candidate_version": candidate.version,
        "baseline_wheel_sha256": baseline.sha256,
        "candidate_wheel_sha256": candidate.sha256,
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "candidate_source_members_verified": members,
        "phases": [
            {k: v for k, v in phase.items() if k not in {"threads", "backup_id"}}
            for phase in (first, second, restored, final)
        ],
        "different_versions_verified": True,
        "fresh_isolated_phase_processes": True,
        "active_owner_backup_refused": True,
        "full_backup_file_count": 7,
        "upgrade_reads_original_thread": True,
        "upgrade_creates_new_thread": True,
        "upgrade_install_preserves_state_bytes": True,
        "post_upgrade_thread_removed_by_restore": True,
        "same_restore_id_does_not_rewind_new_state": True,
        "original_key_retained": True,
        "prior_root_retained": True,
        "rollback_install_preserves_state_bytes": True,
        "rollback_reads_restored_threads": True,
        "rollback_reads_original_backup_threads": True,
        "rollback_matching_backup_restored_before_version_switch": True,
        "candidate_runtime_reopened_after_matching_restore": False,
        "rollback_creates_new_thread": True,
        "workspace_unchanged": True,
        "provider_turn_requests": 0,
        "commercial_release": False,
        "not_proven": ["real_coding_task", "consumer_os_support", "beta", "stable_version_release"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="源码外不同版本升级与完整状态回退验收")
    for name in ("environment-root", "source-root", "wheel", "uv"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    for name in ("source-revision", "wheel-sha256"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--baseline-wheel", type=Path)
    parser.add_argument("--baseline-sha256")
    parser.add_argument("--baseline-source-revision")
    parser.add_argument("--phase", choices=("baseline", "upgraded", "restore", "rollback"))
    parser.add_argument("--before-threads", nargs="*", default=[])
    parser.add_argument("--backup-id", default="")
    parser.add_argument("--restore-id", default="")
    arguments = parser.parse_args()
    try:
        require(
            re.fullmatch(r"[0-9a-f]{40}", arguments.source_revision) is not None,
            "acceptance_input_invalid",
        )
        if arguments.phase:
            result = asyncio.run(_phase(arguments))
        else:
            require(
                arguments.baseline_wheel is not None and arguments.baseline_sha256 is not None,
                "acceptance_input_invalid",
            )
            result = accept_upgrade(arguments)
            target = arguments.environment_root / "result.json"
            with target.open("x", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "code": "upgrade_cancelled"}), file=sys.stderr)
        return 130
    except Exception as error:
        code = str(error) if isinstance(error, AcceptanceFailure) else "upgrade_acceptance_failed"
        print(json.dumps({"status": "failed", "code": code}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
