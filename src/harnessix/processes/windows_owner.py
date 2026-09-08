"""Windows Process owner worker；使用挂起创建和Job Object消除树归属竞态。"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import threading
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
from harnessix.processes.windows_conpty import (
    WindowsConPtyProcess,
    WindowsTtyInputNormalizer,
    spawn_conpty,
)
from harnessix.processes.windows_job import WindowsJobObject

_READ_CHUNK_BYTES = 64 * 1024
_PROGRESS_INTERVAL_SECONDS = 0.25

_Event = tuple[str, bytes | None]


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


def _safe_run_directory(value: str) -> Path:
    path = Path(value)
    info = path.lstat()
    if not path.is_absolute() or not path.is_dir() or path.is_symlink() or not info:
        raise ValueError("invalid run directory")
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
        self.initial_controls = bytearray()
        self._initial_remainder = initial_controls
        self.receipt_path = run_directory / "receipt.json"
        values = tuple(request.environment[name].encode("utf-8") for name in request.secret_names)
        self.stdout = CapturedProcessOutput(run_directory / "stdout.bin", values)
        self.stderr = CapturedProcessOutput(run_directory / "stderr.bin", values)
        self.events: queue.Queue[_Event] = queue.Queue(maxsize=64)
        self.stdin_events: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self.process: subprocess.Popen[bytes] | WindowsConPtyProcess | None = None
        self.job: WindowsJobObject | None = None
        self.started_at: datetime | None = None
        self.stop_reason: ProcessStopReason | None = None
        self.receipt_sequence = 0
        self.last_progress = 0.0
        self.last_published_output = (0, 0, 0, 0)
        self.stdin_accepted = 0
        self.stdin_closed = request.stdin == "closed"
        self.streams_open = {"stdout", "stderr"}
        self.io_failed = False

    def run(self) -> int:
        try:
            self._launch()
        except BaseException:
            if self.process is None:
                self._publish_failed()
            else:
                self.started_at = self.started_at or datetime.now(UTC)
                self._terminate_job()
                self._publish_unknown()
            self._close()
            return 0
        try:
            self._event_loop()
            return 0
        except BaseException:
            self.io_failed = True
            self._terminate_job()
            self._publish_unknown()
            return 1
        finally:
            self._close()

    def _launch(self) -> None:
        if self.request.terminal == "pty":
            conpty = spawn_conpty(
                self.request.argv,
                cwd=self.request.cwd,
                environment=self.request.environment,
                columns=self.request.columns,
                rows=self.request.rows,
            )
            self.process = conpty
            self.job = conpty.job
            self.started_at = datetime.now(UTC)
            self._start_reader("control", self.control_fd, self._initial_remainder)
            assert conpty.output_fd is not None
            self._start_reader("stdout", conpty.output_fd, bytearray())
            self.streams_open = {"stdout"}
            self.stderr.finish(self._remaining_output(), eof=True)
            if self.request.stdin == "pipe":
                threading.Thread(target=self._stdin_writer, daemon=True).start()
            else:
                conpty.close_input()
            self._publish("running")
            return
        flags = (
            subprocess.CREATE_SUSPENDED  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
        self.process = subprocess.Popen(
            self.request.argv,
            cwd=self.request.cwd,
            env=self.request.environment,
            stdin=subprocess.PIPE if self.request.stdin == "pipe" else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=flags,
        )
        self.job = WindowsJobObject()
        try:
            self.job.assign_suspended(self.process.pid)
        except BaseException:
            try:
                self.process.kill()
            finally:
                self.process.wait()
            raise
        if not self.job.contains(self.process.pid):
            self.process.kill()
            self.process.wait()
            raise RuntimeError("target is not in owner job")
        self.started_at = datetime.now(UTC)
        self._start_reader("control", self.control_fd, self._initial_remainder)
        assert self.process.stdout is not None and self.process.stderr is not None
        self._start_reader("stdout", self.process.stdout.fileno(), bytearray())
        self._start_reader("stderr", self.process.stderr.fileno(), bytearray())
        if self.process.stdin is not None:
            threading.Thread(target=self._stdin_writer, daemon=True).start()
        self._publish("running")

    def _start_reader(self, name: str, descriptor: int, initial: bytearray) -> None:
        threading.Thread(
            target=self._reader,
            args=(name, descriptor, initial),
            daemon=True,
        ).start()

    def _reader(self, name: str, descriptor: int, initial: bytearray) -> None:
        try:
            if initial:
                self.events.put((name, bytes(initial)))
            while True:
                data = os.read(descriptor, _READ_CHUNK_BYTES)
                if not data:
                    self.events.put((f"{name}_eof", None))
                    return
                self.events.put((name, data))
        except OSError:
            self.events.put((f"{name}_error", None))

    def _stdin_writer(self) -> None:
        assert self.process is not None
        normalizer = WindowsTtyInputNormalizer()
        try:
            while (data := self.stdin_events.get()) is not None:
                if isinstance(self.process, WindowsConPtyProcess):
                    assert self.process.input_fd is not None
                    view = memoryview(normalizer.normalize(data))
                    while view:
                        written = os.write(self.process.input_fd, view)
                        view = view[written:]
                else:
                    assert self.process.stdin is not None
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
            if isinstance(self.process, WindowsConPtyProcess):
                self.process.close_input()
            else:
                assert self.process.stdin is not None
                self.process.stdin.close()
        except OSError:
            self.events.put(("stdin_error", None))

    def _event_loop(self) -> None:
        assert self.process is not None and self.started_at is not None and self.job is not None
        deadline = self.request.deadline.timestamp()
        while True:
            if self.stop_reason is None and time.time() >= deadline:
                self._request_stop("timeout")
            try:
                name, data = self.events.get(timeout=0.05)
                self._apply_event(name, data)
            except queue.Empty:
                pass
            code = self.process.poll()
            if code is not None and self.stop_reason is None:
                self.stop_reason = "exited"
                self._terminate_job()
            if code is not None and isinstance(self.process, WindowsConPtyProcess):
                self.process.close_pseudoconsole()
            if (
                self._output_position() != self.last_published_output
                and time.monotonic() - self.last_progress >= _PROGRESS_INTERVAL_SECONDS
            ):
                self._publish("running")
            if code is not None and not self.streams_open:
                break
        code = self.process.wait()
        self._finish_streams(eof=True)
        if self.io_failed:
            self._publish_unknown()
        else:
            self._publish("exited", returncode=code, stop_reason=self.stop_reason or "exited")

    def _apply_event(self, name: str, data: bytes | None) -> None:
        if name == "control":
            assert data is not None
            self.initial_controls.extend(data)
            self._consume_controls()
        elif name == "control_eof":
            self._request_stop("host_lost")
        elif name in {"control_error", "stdin_error"}:
            self.io_failed = True
            self._request_stop("io_error")
        elif name in {"stdout", "stderr"}:
            assert data is not None
            stream = self.stdout if name == "stdout" else self.stderr
            stream.feed(data, self._remaining_output())
            if self.stdout.observed + self.stderr.observed > self.request.output_bytes:
                self._request_stop("output_limit")
        elif name in {"stdout_eof", "stderr_eof"}:
            stream_name = cast(Literal["stdout", "stderr"], name.removesuffix("_eof"))
            stream = self.stdout if stream_name == "stdout" else self.stderr
            stream.finish(self._remaining_output(), eof=True)
            self.streams_open.discard(stream_name)
        else:
            self.io_failed = True
            self._request_stop("io_error")

    def _consume_controls(self) -> None:
        if len(self.initial_controls) > MAX_OWNER_CONTROL_FRAME_BYTES:
            self.io_failed = True
            self._request_stop("cleanup_failed")
            return
        while b"\n" in self.initial_controls:
            line, remainder = self.initial_controls.split(b"\n", 1)
            self.initial_controls = bytearray(remainder)
            try:
                command = ProcessOwnerCommand.model_validate_json(line)
            except (ValidationError, ValueError, TypeError):
                self.io_failed = True
                self._request_stop("cleanup_failed")
                return
            if command.operation == "stop":
                assert command.reason is not None
                self._request_stop(command.reason)
            elif command.operation == "resize":
                if not isinstance(self.process, WindowsConPtyProcess):
                    self.io_failed = True
                    self._request_stop("cleanup_failed")
                else:
                    assert command.columns is not None and command.rows is not None
                    self.process.resize(command.columns, command.rows)
            elif command.operation == "close_stdin":
                self._close_stdin()
            else:
                self._queue_stdin(command.data())

    def _queue_stdin(self, data: bytes) -> None:
        if self.stdin_closed or self.stdin_accepted + len(data) > self.request.input_bytes:
            self._request_stop("input_limit")
            return
        self.stdin_accepted += len(data)
        try:
            self.stdin_events.put_nowait(data)
        except queue.Full:
            self._request_stop("input_limit")

    def _close_stdin(self) -> None:
        if self.stdin_closed:
            return
        self.stdin_closed = True
        try:
            self.stdin_events.put_nowait(None)
        except queue.Full:
            self._request_stop("input_limit")

    def _request_stop(self, reason: ProcessStopReason) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self._terminate_job()

    def _terminate_job(self) -> None:
        if self.job is None:
            return
        try:
            self.job.terminate()
        except BaseException:
            self.io_failed = True

    def _remaining_output(self) -> int:
        return max(0, self.request.output_bytes - self.stdout.persisted - self.stderr.persisted)

    def _output_position(self) -> tuple[int, int, int, int]:
        return (
            self.stdout.observed,
            self.stdout.persisted,
            self.stderr.observed,
            self.stderr.persisted,
        )

    def _finish_streams(self, *, eof: bool) -> None:
        if "stdout" in self.streams_open:
            self.stdout.finish(self._remaining_output(), eof=eof)
            self.streams_open.discard("stdout")
        if "stderr" in self.streams_open:
            self.stderr.finish(self._remaining_output(), eof=eof)
            self.streams_open.discard("stderr")

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

    def _publish_unknown(self) -> None:
        self._finish_streams(eof=False)
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

    def _close(self) -> None:
        if self.job is not None:
            self.job.close()
            self.job = None
        if isinstance(self.process, WindowsConPtyProcess):
            self.process.close()
        try:
            os.close(self.control_fd)
        except OSError:
            pass
        self.stdout.close()
        self.stderr.close()


def _control_descriptor(handle: int) -> int:
    if os.name != "nt":
        raise ValueError("Windows owner requires Windows")
    import msvcrt

    return int(msvcrt.open_osfhandle(handle, os.O_RDONLY))  # type: ignore[attr-defined]


def _run(control_handle: int, run_directory: str) -> int:
    if os.name != "nt" or control_handle <= 0:
        return 2
    try:
        directory = _safe_run_directory(run_directory)
        descriptor = _control_descriptor(control_handle)
        request, remainder = _read_start(descriptor)
        if directory.name != str(request.process_id):
            raise ValueError("run directory does not match process id")
        return _Owner(request, descriptor, remainder, directory).run()
    except (OSError, ValueError, ValidationError):
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--control-handle", required=True, type=int)
    parser.add_argument("--run-directory", required=True)
    arguments = parser.parse_args()
    raise SystemExit(_run(arguments.control_handle, arguments.run_directory))


if __name__ == "__main__":
    main()
