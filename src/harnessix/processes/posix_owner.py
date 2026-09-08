"""POSIX Process owner worker；仅由受信Supervisor以私有控制管道启动。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import os
import pty
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from harnessix.processes.owner_output import CapturedProcessOutput
from harnessix.processes.owner_protocol import (
    MAX_OWNER_CONTROL_FRAME_BYTES,
    ProcessOwnerCommand,
    ProcessOwnerStart,
)
from harnessix.processes.owner_receipt import sign_owner_receipt, write_owner_receipt
from harnessix.processes.supervision_contracts import ProcessStopReason

_PROGRESS_INTERVAL_SECONDS = 0.25
_READ_CHUNK_BYTES = 64 * 1024


def _read_start(control_fd: int) -> tuple[ProcessOwnerStart, bytearray]:
    buffer = bytearray()
    while b"\n" not in buffer:
        chunk = os.read(control_fd, min(_READ_CHUNK_BYTES, MAX_OWNER_CONTROL_FRAME_BYTES + 1))
        if not chunk:
            raise ValueError("control closed before start")
        buffer.extend(chunk)
        if len(buffer) > MAX_OWNER_CONTROL_FRAME_BYTES:
            raise ValueError("start frame too large")
    line, remainder = buffer.split(b"\n", 1)
    return ProcessOwnerStart.model_validate_json(line), bytearray(remainder)


def _target_preexec(slave_fd: int | None, expected_parent: int) -> None:
    os.setsid()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG
            os._exit(126)
        if os.getppid() != expected_parent:
            os._exit(126)
    if slave_fd is not None:
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


def _set_terminal_size(descriptor: int, columns: int, rows: int) -> None:
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _signal_group(pid: int, number: int) -> bool:
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _safe_run_directory(value: str) -> Path:
    path = Path(value)
    info = path.lstat()
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError("invalid run directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe run directory")
    return path


class _Owner:
    def __init__(
        self,
        request: ProcessOwnerStart,
        control_fd: int,
        initial_controls: bytearray,
        run_directory: Path,
    ) -> None:
        self.request = request
        self.control_fd = control_fd
        self.controls = initial_controls
        self.run_directory = run_directory
        self.receipt_path = run_directory / "receipt.json"
        values = tuple(request.environment[name].encode("utf-8") for name in request.secret_names)
        self.stdout = CapturedProcessOutput(run_directory / "stdout.bin", values)
        self.stderr = CapturedProcessOutput(run_directory / "stderr.bin", values)
        self.selector = selectors.DefaultSelector()
        self.process: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self.stdin_fd: int | None = None
        self.stdin_pending = bytearray()
        self.stdin_accepted = 0
        self.stdin_closed = request.stdin == "closed"
        self.stdin_close_requested = False
        self.started_at: datetime | None = None
        self.stop_reason: ProcessStopReason | None = None
        self.stop_started: float | None = None
        self.kill_sent = False
        self.control_open = True
        self.streams_open: set[Literal["stdout", "stderr"]] = set()
        self.receipt_sequence = 0
        self.last_progress = 0.0
        self.last_published_output = (0, 0, 0, 0)
        self.io_failed = False

    def run(self) -> int:
        try:
            self._launch()
        except (OSError, ValueError, subprocess.SubprocessError):
            if self.process is None:
                self._publish_failed()
            else:
                self.started_at = self.started_at or datetime.now(UTC)
                self._emergency_cleanup()
                self._publish_unknown()
                self._close_all()
            return 0
        try:
            self._event_loop()
            return 0
        except BaseException:
            self.io_failed = True
            self._request_stop("cleanup_failed")
            self._emergency_cleanup()
            self._publish_unknown()
            return 1
        finally:
            self._close_all()

    def _launch(self) -> None:
        os.set_blocking(self.control_fd, False)
        self.selector.register(self.control_fd, selectors.EVENT_READ, "control")
        parent_pid = os.getpid()
        if self.request.terminal == "pty":
            master, slave = pty.openpty()
            self.master_fd = master
            _set_terminal_size(master, self.request.columns, self.request.rows)
            os.set_blocking(master, False)
            stdin: int = slave if self.request.stdin == "pipe" else subprocess.DEVNULL
            try:
                self.process = subprocess.Popen(
                    self.request.argv,
                    cwd=self.request.cwd,
                    env=self.request.environment,
                    stdin=stdin,
                    stdout=slave,
                    stderr=slave,
                    shell=False,
                    close_fds=True,
                    preexec_fn=lambda: _target_preexec(slave, parent_pid),
                )
            finally:
                os.close(slave)
            self.stdin_fd = master if self.request.stdin == "pipe" else None
            self.streams_open.add("stdout")
            self.selector.register(master, selectors.EVENT_READ, "stdout")
            self.stderr.finish(self._remaining_output(), eof=True)
        else:
            self.process = subprocess.Popen(
                self.request.argv,
                cwd=self.request.cwd,
                env=self.request.environment,
                stdin=subprocess.PIPE if self.request.stdin == "pipe" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                preexec_fn=lambda: _target_preexec(None, parent_pid),
            )
            assert self.process.stdout is not None and self.process.stderr is not None
            for raw_name, stream in (
                ("stdout", self.process.stdout),
                ("stderr", self.process.stderr),
            ):
                name = cast(Literal["stdout", "stderr"], raw_name)
                os.set_blocking(stream.fileno(), False)
                self.streams_open.add(name)
                self.selector.register(stream.fileno(), selectors.EVENT_READ, name)
            if self.process.stdin is not None:
                self.stdin_fd = self.process.stdin.fileno()
                os.set_blocking(self.stdin_fd, False)
        self.started_at = datetime.now(UTC)
        self._publish("running")

    def _event_loop(self) -> None:
        assert self.process is not None and self.started_at is not None
        deadline = self.request.deadline.timestamp()
        while True:
            now = time.time()
            if self.stop_reason is None and now >= deadline:
                self._request_stop("timeout")
            self._escalate_if_needed()
            for key, mask in self.selector.select(0.05):
                if key.data == "control":
                    self._read_controls()
                elif key.fd == self.stdin_fd and mask & selectors.EVENT_WRITE:
                    self._flush_stdin()
                    if not mask & selectors.EVENT_READ:
                        continue
                    self._read_stream(key.fd, "stdout")
                elif key.data == "stdin":
                    self._flush_stdin()
                elif mask & selectors.EVENT_READ:
                    self._read_stream(key.fd, key.data)
            code = self.process.poll()
            if code is not None and self.stop_reason is None:
                self.stop_reason = "exited"
                if _group_exists(self.process.pid):
                    self.stop_started = time.monotonic()
                    _signal_group(self.process.pid, signal.SIGTERM)
            if (
                self._output_position() != self.last_published_output
                and time.monotonic() - self.last_progress >= _PROGRESS_INTERVAL_SECONDS
            ):
                self._publish("running")
            if code is not None and not self.streams_open and not _group_exists(self.process.pid):
                break
        self._finish_streams(eof=True)
        code = self.process.wait()
        if self.io_failed or self.stop_reason == "cleanup_failed":
            self._publish_unknown()
        else:
            self._publish("exited", returncode=code, stop_reason=self.stop_reason or "exited")

    def _read_controls(self) -> None:
        try:
            data = os.read(self.control_fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        if not data:
            self.control_open = False
            self.selector.unregister(self.control_fd)
            self._request_stop("host_lost")
            return
        self.controls.extend(data)
        if len(self.controls) > MAX_OWNER_CONTROL_FRAME_BYTES:
            self.io_failed = True
            self._request_stop("cleanup_failed")
            return
        while b"\n" in self.controls:
            line, remainder = self.controls.split(b"\n", 1)
            self.controls = bytearray(remainder)
            try:
                command = ProcessOwnerCommand.model_validate_json(line)
            except (ValidationError, ValueError, TypeError):
                self.io_failed = True
                self._request_stop("cleanup_failed")
                return
            self._apply(command)

    def _apply(self, command: ProcessOwnerCommand) -> None:
        if command.operation == "stop":
            assert command.reason is not None
            self._request_stop(command.reason)
        elif command.operation == "close_stdin":
            self._close_stdin()
        elif command.operation == "resize":
            if self.master_fd is None:
                self.io_failed = True
                self._request_stop("cleanup_failed")
            else:
                assert command.columns is not None and command.rows is not None
                _set_terminal_size(self.master_fd, command.columns, command.rows)
        else:
            payload = command.data()
            if self.stdin_closed or self.stdin_accepted + len(payload) > self.request.input_bytes:
                self._request_stop("input_limit")
                return
            self.stdin_accepted += len(payload)
            self.stdin_pending.extend(payload)
            self._watch_stdin()

    def _watch_stdin(self) -> None:
        if self.stdin_fd is None or not self.stdin_pending:
            return
        try:
            key = self.selector.get_key(self.stdin_fd)
        except KeyError:
            self.selector.register(self.stdin_fd, selectors.EVENT_WRITE, "stdin")
        else:
            self.selector.modify(self.stdin_fd, key.events | selectors.EVENT_WRITE, key.data)

    def _flush_stdin(self) -> None:
        if self.stdin_fd is None or not self.stdin_pending:
            return
        try:
            written = os.write(self.stdin_fd, self.stdin_pending)
        except BlockingIOError:
            return
        except OSError:
            self.io_failed = True
            self._request_stop("io_error")
            return
        del self.stdin_pending[:written]
        if not self.stdin_pending:
            if self.master_fd == self.stdin_fd:
                self.selector.modify(self.stdin_fd, selectors.EVENT_READ, "stdout")
            else:
                self.selector.unregister(self.stdin_fd)
            if self.stdin_close_requested:
                self.stdin_close_requested = False
                self._close_stdin()

    def _close_stdin(self) -> None:
        if self.stdin_closed:
            return
        if self.stdin_pending:
            self.stdin_close_requested = True
            return
        self.stdin_closed = True
        if self.stdin_fd is None:
            return
        if self.master_fd == self.stdin_fd:
            try:
                os.write(self.stdin_fd, b"\x04")
            except OSError:
                pass
        else:
            try:
                self.selector.unregister(self.stdin_fd)
            except KeyError:
                pass
            os.close(self.stdin_fd)
            self.stdin_fd = None

    def _read_stream(self, descriptor: int, name: Literal["stdout", "stderr"]) -> None:
        try:
            data = os.read(descriptor, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError as error:
            if self.master_fd == descriptor and error.errno == errno.EIO:
                data = b""
            else:
                self.io_failed = True
                self._request_stop("io_error")
                return
        if data:
            stream = self.stdout if name == "stdout" else self.stderr
            stream.feed(data, self._remaining_output())
            if self.stdout.observed + self.stderr.observed > self.request.output_bytes:
                self._request_stop("output_limit")
            return
        self._close_stream(descriptor, name, eof=True)

    def _close_stream(
        self, descriptor: int, name: Literal["stdout", "stderr"], *, eof: bool
    ) -> None:
        try:
            self.selector.unregister(descriptor)
        except KeyError:
            pass
        if self.master_fd == descriptor:
            os.close(descriptor)
            self.master_fd = None
            self.stdin_fd = None
        stream = self.stdout if name == "stdout" else self.stderr
        stream.finish(self._remaining_output(), eof=eof)
        self.streams_open.discard(name)

    def _finish_streams(self, *, eof: bool) -> None:
        for name in tuple(self.streams_open):
            descriptor = self._stream_descriptor(name)
            if descriptor is not None:
                self._close_stream(descriptor, name, eof=eof)
        if not self.stdout.eof and "stdout" not in self.streams_open:
            self.stdout.finish(self._remaining_output(), eof=eof)
        if not self.stderr.eof and "stderr" not in self.streams_open:
            self.stderr.finish(self._remaining_output(), eof=eof)

    def _stream_descriptor(self, name: Literal["stdout", "stderr"]) -> int | None:
        if self.master_fd is not None:
            return self.master_fd if name == "stdout" else None
        if self.process is None:
            return None
        stream = self.process.stdout if name == "stdout" else self.process.stderr
        return None if stream is None or stream.closed else stream.fileno()

    def _remaining_output(self) -> int:
        return max(0, self.request.output_bytes - self.stdout.persisted - self.stderr.persisted)

    def _output_position(self) -> tuple[int, int, int, int]:
        return (
            self.stdout.observed,
            self.stdout.persisted,
            self.stderr.observed,
            self.stderr.persisted,
        )

    def _request_stop(self, reason: ProcessStopReason) -> None:
        if self.stop_reason is not None:
            return
        self.stop_reason = reason
        if self.process is not None:
            self.stop_started = time.monotonic()
            if not _signal_group(self.process.pid, signal.SIGTERM):
                self.io_failed = True

    def _escalate_if_needed(self) -> None:
        if (
            self.process is None
            or self.stop_started is None
            or self.kill_sent
            or time.monotonic() - self.stop_started < self.request.terminate_grace_seconds
        ):
            return
        if _group_exists(self.process.pid):
            self.kill_sent = True
            if not _signal_group(self.process.pid, signal.SIGKILL):
                self.io_failed = True

    def _publish(
        self,
        state: Literal["running", "exited"],
        *,
        returncode: int | None = None,
        stop_reason: ProcessStopReason | None = None,
    ) -> None:
        assert self.process is not None and self.started_at is not None
        self.stdout.sync()
        self.stderr.sync()
        self.receipt_sequence += 1
        receipt = sign_owner_receipt(
            process_id=self.request.process_id,
            owner_identity=self.request.owner_identity,
            state=state,
            sequence=self.receipt_sequence,
            owner_token=self.request.owner_token,
            pid=self.process.pid,
            started_at=self.started_at,
            finished_at=datetime.now(UTC) if state == "exited" else None,
            returncode=returncode,
            stop_reason=stop_reason,
            stdout=self.stdout.observation(),
            stderr=self.stderr.observation(),
        )
        write_owner_receipt(self.receipt_path, receipt)
        self.last_progress = time.monotonic()
        self.last_published_output = self._output_position()

    def _publish_failed(self) -> None:
        self._finish_streams(eof=True)
        self.stdout.sync()
        self.stderr.sync()
        receipt = sign_owner_receipt(
            process_id=self.request.process_id,
            owner_identity=self.request.owner_identity,
            state="failed",
            sequence=1,
            owner_token=self.request.owner_token,
            finished_at=datetime.now(UTC),
            stop_reason="launch_failed",
            stdout=self.stdout.observation(),
            stderr=self.stderr.observation(),
        )
        write_owner_receipt(self.receipt_path, receipt)
        self._close_all()

    def _publish_unknown(self) -> None:
        self._finish_streams(eof=False)
        self.stdout.sync()
        self.stderr.sync()
        self.receipt_sequence += 1
        receipt = sign_owner_receipt(
            process_id=self.request.process_id,
            owner_identity=self.request.owner_identity,
            state="unknown",
            sequence=max(1, self.receipt_sequence),
            owner_token=self.request.owner_token,
            pid=None if self.process is None else self.process.pid,
            started_at=self.started_at,
            finished_at=datetime.now(UTC),
            stop_reason="cleanup_failed",
            stdout=self.stdout.observation(),
            stderr=self.stderr.observation(),
        )
        write_owner_receipt(self.receipt_path, receipt)

    def _emergency_cleanup(self) -> None:
        if self.process is None:
            return
        _signal_group(self.process.pid, signal.SIGKILL)
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.io_failed = True

    def _close_all(self) -> None:
        self.selector.close()
        for descriptor in (self.control_fd, self.master_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.master_fd = None
        self.stdin_fd = None
        self.stdout.close()
        self.stderr.close()


def _run(control_fd: int, run_directory: str) -> int:
    if os.name != "posix" or control_fd < 3:
        return 2
    try:
        directory = _safe_run_directory(run_directory)
        request, remainder = _read_start(control_fd)
        if directory.name != str(request.process_id):
            raise ValueError("run directory does not match process id")
        return _Owner(request, control_fd, remainder, directory).run()
    except (OSError, ValueError, ValidationError):
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--control-fd", required=True, type=int)
    parser.add_argument("--run-directory", required=True)
    arguments = parser.parse_args()
    raise SystemExit(_run(arguments.control_fd, arguments.run_directory))


if __name__ == "__main__":
    main()
