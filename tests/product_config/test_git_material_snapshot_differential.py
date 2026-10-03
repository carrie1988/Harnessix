"""真实快照的无写／写入与namespace持有对照；不产生Owner或产品交付证明。"""

from __future__ import annotations

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
from harnessix.delivery.git_material_native import _namespace, _Resources, _snapshot
from harnessix.delivery.git_material_native_windows import _Windows
from harnessix.delivery.git_material_worker import _command, _git
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


@pytest.mark.parametrize(("write", "held"), CASES)
def test_real_snapshot_hash_and_write(make_process, tmp_path, write, held) -> None:
    executable = _checked_executable()
    case = make_process(executable=executable, output_redaction=material_tests._Protection())
    binding = material_tests._repository(case, tmp_path, "sha256")
    body = material_tests._body(binding, "commit", 0)
    material = material_tests._material(body, "sha256", "commit")
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
    assert _checked_executable() == executable
