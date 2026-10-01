"""Git 专用受控子程序：小握手、完整 RO 快照和固定对象写入，不拥有产品批准。

宿主以原 Owner 已验真冻结的同一保护快照扫描完整正文。本模块完整 SHA 验真
继承该谓词，不自称扫描保护值，也不自称 Owner 直接观察了 Git stdin。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO

from harnessix.delivery.git_material_failure import (
    GitMaterialFailureObservation,
    encode_failure_observation,
    observe_failure_before_cleanup,
)
from harnessix.delivery.git_material_input_contracts import (
    MAX_MANIFEST_BYTES,
    MAX_MATERIAL_BYTES,
    GitMaterialInput,
    GitMaterialInputError,
    GitMaterialProof,
    decode_manifest,
    encode_proof,
    executable_identity,
    implementation_digest,
    manifest_sha256,
)
from harnessix.delivery.git_material_native import (
    _descriptor,
    _directory,
    _fail,
    _namespace,
    _remaining,
    _Resources,
    _snapshot,
)
from harnessix.delivery.git_material_native import (
    capture_control_files as capture_control_files,
)
from harnessix.delivery.git_material_native_windows import _Windows


def fixed_git_argv(
    executable: str, common_path: str, hooks_path: str, object_type: str
) -> tuple[str, ...]:
    """显式选择已绑定common，不借cwd发现另一个对象库。"""
    if type(object_type) is not str or object_type not in {"blob", "tree", "commit"}:
        _fail()
    return (
        executable,
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "color.ui=false",
        "-c",
        "core.hooksPath=" + hooks_path,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "--git-dir=" + common_path,
        "hash-object",
        "--no-filters",
        "-t",
        object_type,
        "-w",
        "--stdin",
    )


def _command(request: GitMaterialInput, resources: _Resources, windows: _Windows | None) -> None:
    argv = request.git_argv
    if len(argv) != 22 or not argv[6].startswith("core.hooksPath="):
        _fail()
    hooks = Path(argv[6].split("=", 1)[1])
    if argv != fixed_git_argv(argv[0], request.common_path, str(hooks), request.object_type):
        _fail()
    _directory(hooks, resources, windows, private=True)
    if any(hooks.iterdir()):
        _fail("git_material_namespace_invalid")
    environment = dict(request.git_environment)
    expected = {
        "PATH": os.pathsep.join((str(Path(argv[0]).parent), os.defpath)),
        "HOME": environment.get("HOME", ""),
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
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "file",
        "TMPDIR": environment.get("TMPDIR", ""),
        "TEMP": environment.get("TMPDIR", ""),
        "TMP": environment.get("TMPDIR", ""),
    }
    if windows is not None:
        system = os.environ.get("SystemRoot", r"C:\Windows")
        expected.update(
            SystemRoot=system, WINDIR=system, COMSPEC=str(Path(system) / "System32/cmd.exe")
        )
    if environment != expected:
        _fail()
    for path in (Path(expected["HOME"]), Path(expected["TMPDIR"])):
        if not path.is_absolute():
            _fail()
        _directory(path, resources, windows, private=True)
    _directory(Path(argv[0]).parent, resources, windows)
    _descriptor(Path(argv[0]), resources, windows)
    if executable_identity(argv[0]) != request.git_executable_identity:
        _fail("git_material_binding_changed")


def _child_preexec(parent: int) -> None:
    """Linux父死亡保护只加在新边，不调用setsid破坏原Owner进程组。"""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, 9) != 0 or os.getppid() != parent:
        os._exit(126)


def _git(
    request: GitMaterialInput, snapshot: BinaryIO, observation: GitMaterialFailureObservation
) -> str:
    observation.stage = "git_launch"
    _remaining(request.expiry_monotonic_ns)
    parent_pid = os.getpid()
    preexec = (lambda: _child_preexec(parent_pid)) if sys.platform.startswith("linux") else None
    # 不设置setsid/new-session/breakaway；Git留在原Supervisor的进程树内。
    with (
        subprocess.Popen(
            request.git_argv,
            cwd=request.repo_path,
            env=dict(request.git_environment),
            stdin=snapshot,
            stdout=subprocess.PIPE,
            stderr=sys.stderr.buffer,
            shell=False,
            close_fds=True,
            preexec_fn=preexec,
        ) as process,
        observe_failure_before_cleanup(observation, "git_cleanup"),
    ):
        observation.git_popen_returned = True
        assert process.stdout is not None
        stdout = process.stdout
        output: list[bytes] = []
        failed = threading.Event()

        def reader() -> None:
            try:
                body = stdout.read(66)
                if len(body) >= 66:
                    failed.set()
                    process.kill()
                output.append(body)
            except OSError:
                failed.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            with observe_failure_before_cleanup(observation, "git_cleanup"):
                observation.stage = "git_wait"
                code = process.wait(timeout=_remaining(request.expiry_monotonic_ns))
                observation.git_returncode = code
                observation.stage = "git_join"
                thread.join(timeout=_remaining(request.expiry_monotonic_ns))
                observation.stage = "git_validate"
                # 保持原短路顺序；非零退出不补查原未求值的输出长度。
                if thread.is_alive():
                    observation.git_stdout_complete = False
                    _fail("git_material_git_failed")
                if failed.is_set():
                    observation.git_stdout_complete = False
                    _fail("git_material_git_failed")
                if code != 0:
                    _fail("git_material_git_failed")
                complete = len(output) == 1
                observation.git_stdout_complete = complete
                if not complete:
                    _fail("git_material_git_failed")
                expected = (request.expected_oid + "\n").encode("ascii")
                matched = output[0] == expected
                observation.git_stdout_expected = matched
                if not matched:
                    _fail("git_material_git_failed")
                return request.expected_oid
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def run_worker(
    payload: bytes,
    *,
    expected_manifest_sha256: str,
    expected_nonce: str,
    expiry_monotonic_ns: int,
    observation: GitMaterialFailureObservation | None = None,
) -> GitMaterialProof:
    """仅供固定worker入口使用；不是替代产品Supervisor的执行成功接口。"""
    observation = (
        observation
        if type(observation) is GitMaterialFailureObservation
        else GitMaterialFailureObservation()
    )
    observation.stage = "launch_binding"
    _remaining(expiry_monotonic_ns)
    request = decode_manifest(payload)
    if (
        request.nonce != expected_nonce
        or request.expiry_monotonic_ns != expiry_monotonic_ns
        or manifest_sha256(request) != expected_manifest_sha256
        or request.implementation_digest != implementation_digest()
    ):
        _fail("git_material_launch_mismatch")
    windows = _Windows() if os.name == "nt" else None
    if os.name not in {"posix", "nt"}:
        _fail("git_material_platform_unsupported")
    with _Resources() as resources, observe_failure_before_cleanup(observation, "resources_close"):
        observation.stage = "command"
        _command(request, resources, windows)
        observation.stage = "namespace"
        _namespace(request, resources, windows)
        observation.stage = "snapshot"
        snapshot = _snapshot(request, resources, windows)
        _remaining(expiry_monotonic_ns)
        oid = _git(request, snapshot, observation)
        observation.stage = "snapshot_recheck"
        # Git已退出后再次观察完整snapshot；POSIX匿名句柄只有本次worker及Git读端。
        snapshot.seek(0)
        # 匿名snapshot允许nlink=0，所以直接有界复读，不借原stage路径。
        body = snapshot.read(MAX_MATERIAL_BYTES + 1)
        if (
            len(body) != request.body_bytes
            or hashlib.sha256(body).hexdigest() != request.body_sha256
        ):
            _fail("git_material_body_changed")
        observation.stage = "command_recheck"
        _command(request, resources, windows)
        observation.stage = "namespace_recheck"
        _namespace(request, resources, windows)
        _remaining(expiry_monotonic_ns)
        observation.stage = "proof"
        return GitMaterialProof(
            nonce=request.nonce,
            source_digest=request.source_digest,
            manifest_sha256=manifest_sha256(request),
            purpose_digest=request.purpose_digest,
            implementation_digest=request.implementation_digest,
            body_sha256=request.body_sha256,
            body_bytes=request.body_bytes,
            source_eof=True,
            snapshot_sha256=hashlib.sha256(body).hexdigest(),
            snapshot_bytes=len(body),
            object_id=oid,
            object_format=request.object_format,
            git_stdout_eof=True,
            git_returncode=0,
            producer_pid=os.getpid(),
            object_type=request.object_type,
        )


def main(argv: list[str] | None = None) -> int:
    """只接受固定启动绑定；失败输出固定码，不输出参数、body或第三方异常。"""
    observation = GitMaterialFailureObservation()
    try:
        observation.stage = "stream_mode"
        if os.name == "nt":
            import msvcrt

            for stream in (sys.stdin, sys.stdout, sys.stderr):
                msvcrt.__dict__["setmode"](stream.fileno(), getattr(os, "O_BINARY", 0))
        observation.stage = "arguments"
        arguments = sys.argv[1:] if argv is None else argv
        if (
            type(arguments) is not list
            or len(arguments) != 6
            or not all(type(item) is str for item in arguments)
        ):
            _fail()
        if arguments[::2] != ["--manifest-sha256", "--nonce", "--expiry-monotonic-ns"]:
            _fail()
        digest, nonce, expiry_text = arguments[1::2]
        if (
            re.fullmatch(r"[0-9a-f]{64}", nonce) is None
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or re.fullmatch(r"[1-9][0-9]{0,18}", expiry_text) is None
        ):
            _fail()
        expiry = int(expiry_text)
        if expiry > 2**63 - 1:
            _fail()
        _remaining(expiry)
        # EOF是小握手的一部分；原Owner监控同操作上界，宿主不close时不启动Git。
        observation.stage = "input"
        payload = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        proof = run_worker(
            payload,
            expected_manifest_sha256=digest,
            expected_nonce=nonce,
            expiry_monotonic_ns=expiry,
            observation=observation,
        )
        observation.stage = "proof_output"
        sys.stdout.buffer.write(encode_proof(proof))
        sys.stdout.buffer.flush()
        return 0
    except (
        GitMaterialInputError,
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        try:
            frame = encode_failure_observation(observation, error)
        except Exception:
            # 纯诊断失败退回原marker；业务不重试，原捕获和sink语义不变。
            frame = b""
        try:
            sys.stderr.buffer.write(b"git_material_worker_failed\n" + frame)
            sys.stderr.buffer.flush()
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
