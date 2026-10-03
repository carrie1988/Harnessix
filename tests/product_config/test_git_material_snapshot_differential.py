"""真实快照的无写／写入与namespace持有对照；不产生Owner或产品交付证明。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import stat
import time
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.delivery.git_material_failure import GitMaterialFailureObservation
from harnessix.delivery.git_material_input_contracts import GitMaterialInput
from harnessix.delivery.git_material_native import (
    _descriptor,
    _directory,
    _namespace,
    _Resources,
    _snapshot,
)
from harnessix.delivery.git_material_native_windows import _api_path, _Windows
from harnessix.delivery.git_material_worker import _command, _git
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_material_process import stage_material
from scripts.windows_git_native_branch_observation import contract, identity, preflight
from tests.product_config import test_git_material_input as material_tests

ROOT = Path(__file__).resolve().parents[2]
make_process = material_tests.make_process
CASES = (
    pytest.param(False, False, id="hash-direct"),
    pytest.param(False, True, id="hash-held"),
    pytest.param(True, False, id="write-direct"),
    pytest.param(True, True, id="write-held"),
)


def _checked_executable() -> Path:
    """Windows显式诊断必须复用本Run原PE／PDB和18输入，缺少身份不得降级。"""
    if os.name != "nt":
        selected = shutil.which("git")
        assert selected is not None, "真实对照需要已安装的SHA256 Git"
        return Path(selected).resolve(strict=True)
    if os.environ.get("HARNESSIX_GIT_NATIVE_DIFFERENTIAL") != "1":
        pytest.skip("Windows发行身份对照仅在显式手动诊断运行")
    symbols = Path(os.environ["HARNESSIX_GIT_NATIVE_DIFFERENTIAL_SYMBOLS"])
    assert symbols.is_absolute(), "官方符号根必须为本Run的绝对私有路径"
    fixed = contract.read_contract()
    contract.source_checks(ROOT, fixed)
    selected, paths = preflight.selected_paths()
    for row in fixed["pairs"]:
        identity.check_pair(paths[row["role"]], symbols / row["role"] / "git.pdb", row)
    return selected


@dataclass(frozen=True, slots=True)
class _GitInvocation:
    """仅供直接Git helper的测试参数，不是manifest、批准请求或业务Proof。"""

    git_argv: tuple[str, ...]
    repo_path: str
    git_environment: tuple[tuple[str, str], ...]
    expiry_monotonic_ns: int
    expected_oid: str


def _diagnostic_request(request: GitMaterialInput, *, write: bool) -> _GitInvocation:
    """不改正式请求，只派生测试调用参数；原操作与20秒命令期限取较早者。"""
    assert request.git_argv.count("-w") == 1
    argv = request.git_argv if write else tuple(arg for arg in request.git_argv if arg != "-w")
    return _GitInvocation(
        git_argv=argv,
        repo_path=request.repo_path,
        git_environment=request.git_environment,
        expiry_monotonic_ns=min(request.expiry_monotonic_ns, time.monotonic_ns() + 20_000_000_000),
        expected_oid=request.expected_oid,
    )


def _object_bytes(objects: Path) -> tuple[tuple[str, str], ...]:
    """仅冻结fresh测试库普通文件字节，用于无写臂的效果断言。"""
    return tuple(
        sorted(
            (str(path.relative_to(objects)), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in objects.rglob("*")
            if path.is_file()
        )
    )


def _hold_object_target(request, material, resources, windows, scope) -> None:
    """只新增一个真实目标持有；Windows不额外打开其父链。"""
    assert scope in {"fanout", "blob"}
    blob = material_tests._material(b"baseline\n", "sha256", "blob")
    assert blob.object_id[:2] == material.object_id[:2] == "91"
    assert blob.object_id != material.object_id
    directory = Path(request.objects_path) / material.object_id[:2]
    baseline = directory / blob.object_id[2:]
    assert directory.is_dir() and baseline.is_file()
    target = directory if scope == "fanout" else baseline
    if windows is not None:
        windows.open(target, resources, directory=scope == "fanout")
    elif scope == "fanout":
        _directory(target, resources, None)
    else:
        _descriptor(target, resources, None)


def _run_diagnostic(case, binding, material, *, write, held, extra_hold=None) -> GitOperationBudget:
    """原四格及新配对共用真实操作；不更新manifest或批准证明。"""
    assert not (held and extra_hold is not None)
    assert extra_hold is None or (write and extra_hold in {"fanout", "blob"})
    body = material.body
    prepared = material_tests._prepare(case, binding, material)
    assert prepared.write is not None
    request = prepared.write.request
    objects = Path(request.objects_path)
    target = objects / material.object_id[:2] / material.object_id[2:]
    assert not target.exists(), "目标必须事先不存在，避免把已有对象当成新写入"
    before = _object_bytes(objects)
    material_tests.process_tests._assert_not_started(case)
    staged = stage_material(prepared.write, CancelToken(), prepared.budget)
    try:
        with _Resources() as resources:
            windows = _Windows() if os.name == "nt" else None
            _command(request, resources, windows)
            if held:
                _namespace(request, resources, windows)
            if extra_hold is not None:
                _hold_object_target(request, material, resources, windows, extra_hold)
            snapshot = _snapshot(request, resources, windows)
            info = os.fstat(snapshot.fileno())
            assert stat.S_ISREG(info.st_mode) and info.st_size == len(body)
            assert os.lseek(snapshot.fileno(), 0, os.SEEK_CUR) == 0
            with pytest.raises(OSError):
                os.write(snapshot.fileno(), b"!")
            assert os.lseek(snapshot.fileno(), 0, os.SEEK_CUR) == 0
            # 不增加新的启动器或读取stderr；原_git仍校验完整OID及66字节上限。
            with open(os.devnull, "w", encoding="ascii") as null, redirect_stderr(null):
                observed = _git(
                    _diagnostic_request(request, write=write),
                    snapshot,
                    GitMaterialFailureObservation(),
                )
            assert observed == material.object_id
        assert not tuple(Path(request.stage_root).glob("snapshot-*.bin"))
    finally:
        staged.remove()
    prepared.budget.remaining()
    if write:
        assert target.is_file(), "写臂必须真正新增目标对象"
        returned = case.runner.run(
            case.workspace,
            ("cat-file", "commit", material.object_id),
            timeout=min(20.0, prepared.budget.remaining()),
        )
        assert returned.stdout == body
    else:
        assert not target.exists() and _object_bytes(objects) == before
    prepared.budget.remaining()
    return prepared.budget


@pytest.mark.parametrize(("write", "held"), CASES)
def test_real_snapshot_hash_and_write(make_process, tmp_path, write, held) -> None:
    executable = _checked_executable()
    case = make_process(executable=executable, output_redaction=material_tests._Protection())
    binding = material_tests._repository(case, tmp_path, "sha256")
    material = material_tests._material(
        material_tests._body(binding, "commit", 0), "sha256", "commit"
    )
    budget = _run_diagnostic(case, binding, material, write=write, held=held)
    assert _checked_executable() == executable
    budget.remaining()


def _object_directories(objects: Path) -> tuple[str, ...]:
    return tuple(
        sorted(str(path.relative_to(objects)) for path in objects.rglob("*") if path.is_dir())
    )


def _same_seed_repositories(make_process, tmp_path, executable):
    """先复制两臂再执行任何写入；完整初始对象相等且物理文件不共用。"""
    seed = make_process(executable=executable, output_redaction=material_tests._Protection())
    seed_root = tmp_path / "seed-binding"
    seed_root.mkdir(mode=0o700)
    binding = material_tests._repository(seed, seed_root, "sha256")
    seed_objects = seed.workspace / ".git/objects"
    before = _object_bytes(seed_objects), _object_directories(seed_objects)
    pairs = []
    for arm in ("direct", "held"):
        case = make_process(executable=executable, output_redaction=material_tests._Protection())
        shutil.copytree(
            seed.workspace, case.workspace, dirs_exist_ok=True, copy_function=shutil.copy2
        )
        objects = case.workspace / ".git/objects"
        assert (_object_bytes(objects), _object_directories(objects)) == before
        for relative, _ in before[0]:
            copied, original = (objects / relative).stat(), (seed_objects / relative).stat()
            assert copied.st_nlink == original.st_nlink == 1
            assert (copied.st_dev, copied.st_ino) != (original.st_dev, original.st_ino)
        root = tmp_path / f"{arm}-binding"
        root.mkdir(mode=0o700)
        rebound = material_tests._repository_binding_existing(case, root)
        assert rebound.head_tree_oid == binding.head_tree_oid
        pairs.append((case, rebound))
    return pairs


@pytest.mark.parametrize("scope", ["fanout", "blob"])
def test_same_seed_repository_with_single_hold(make_process, tmp_path, scope) -> None:
    executable = _checked_executable()
    pairs = _same_seed_repositories(make_process, tmp_path, executable)
    bodies = [material_tests._body(binding, "commit", 0) for _, binding in pairs]
    assert bodies[0] == bodies[1]
    material = material_tests._material(bodies[0], "sha256", "commit")
    for case, _ in pairs:
        assert not (
            case.workspace / ".git/objects" / material.object_id[:2] / material.object_id[2:]
        ).exists()
    for index, (case, binding) in enumerate(pairs):
        budget = _run_diagnostic(
            case, binding, material, write=True, held=False, extra_hold=scope if index else None
        )
        assert _checked_executable() == executable
        budget.remaining()


def _write_child(path: Path) -> bytes:
    """真实CRT排他创建；0444不改变创建FD已有的读写权限。"""
    body = b"fresh-object\n"
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o444
    )
    try:
        assert os.write(descriptor, body) == len(body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert path.read_bytes() == body
    return body


def _link_result(windows: _Windows, source: Path, target: Path) -> tuple[bool, int]:
    """紧邻真实Win32调用取得本线程错误，仅供固定断言，不作日志投影。"""
    create = windows.kernel.CreateHardLinkW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
    create.restype = ctypes.c_int
    ctypes.__dict__["set_last_error"](0)
    result = bool(create(_api_path(target), _api_path(source), None))
    return result, 0 if result else ctypes.__dict__["get_last_error"]()


def _checked_local_directory(windows: _Windows, path: Path) -> None:
    # 原chain验证本地NTFS；校验栈先退出，不能污染无持有控制。
    with _Resources() as resources:
        windows.chain(path, resources, resources.directories)


@pytest.mark.skipif(os.name != "nt", reason="需要真实Windows本地NTFS；POSIX不计通过")
@pytest.mark.parametrize("operation", ["create", "link"])
def test_real_windows_child_operation_under_fanout_hold(tmp_path, operation) -> None:
    executable = _checked_executable()
    windows = _Windows()
    directories = [tmp_path / arm / "91" for arm in ("direct", "held")]
    for directory in directories:
        directory.mkdir(parents=True)
        (directory / "baseline").write_bytes(b"baseline\n")
        _checked_local_directory(windows, directory)
    assert _object_bytes(directories[0]) == _object_bytes(directories[1])
    for index, directory in enumerate(directories):
        budget = GitOperationBudget(45)
        with _Resources() as resources:
            if index:
                windows.open(directory, resources, directory=True)
            source = directory / "tmp_obj_control"
            body = _write_child(source)
            if operation == "link":
                target = directory / "new-object"
                assert _link_result(windows, source, target) == (True, 0)
                assert source.stat().st_ino == target.stat().st_ino
                assert target.read_bytes() == body
        budget.remaining()
        assert _checked_executable() == executable
        budget.remaining()


@pytest.mark.skipif(os.name != "nt", reason="需要真实Windows共享检查；POSIX不计通过")
def test_real_windows_link_sharing_counterfactual(tmp_path, monkeypatch) -> None:
    """可证伪因果反例：原目录share1拒绝，唯独share3变化后真实链接成功。"""
    executable = _checked_executable()
    windows = _Windows()
    directory = tmp_path / "91"
    directory.mkdir()
    _checked_local_directory(windows, directory)
    source, target = directory / "tmp_obj_control", directory / "new-object"
    budget = GitOperationBudget(45)
    body = _write_child(source)
    assert _link_result(windows, source, target) == (True, 0)
    assert source.stat().st_ino == target.stat().st_ino and target.read_bytes() == body
    os.chmod(target, 0o600)
    target.unlink()
    os.chmod(source, 0o444)
    with _Resources() as resources:
        windows.open(directory, resources, directory=True)
        assert _link_result(windows, source, target) == (False, 32)
        assert not target.exists() and source.read_bytes() == body
    budget.remaining()
    original_open = windows.kernel.CreateFileW
    opened = []

    def directory_write_share(path, access, share, security, disposition, flags, template):
        # 只改这个api实例的一次目录共享参数；原DLL、完整原验权及句柄清理不变。
        assert (path, access, share, security, disposition, flags, template) == (
            _api_path(directory),
            0x81,
            1,
            None,
            3,
            0x2200000,
            None,
        )
        opened.append((access, share | 2))
        assert len(opened) == 1
        return original_open(path, access, share | 2, security, disposition, flags, template)

    with monkeypatch.context() as patch:
        patch.setattr(windows.kernel, "CreateFileW", directory_write_share)
        with _Resources() as resources:
            windows.open(directory, resources, directory=True)
            assert _link_result(windows, source, target) == (True, 0)
            assert source.stat().st_ino == target.stat().st_ino and target.read_bytes() == body
    assert opened == [(0x81, 3)]
    budget.remaining()
    assert _checked_executable() == executable
    budget.remaining()
