import os

from harnessix.processes.windows_conpty import WindowsConPtyProcess, WindowsTtyInputNormalizer


def test_windows_tty_input_normalizes_newlines_backspace_and_chunk_boundary() -> None:
    normalizer = WindowsTtyInputNormalizer()
    assert normalizer.normalize("中\r".encode()) == "中\r".encode()
    assert normalizer.normalize(b"\nnext\nX\x08") == b"next\rX\x7f"


def test_windows_conpty_eof_uses_console_key_without_closing_transport() -> None:
    read_fd, write_fd = os.pipe()
    process = object.__new__(WindowsConPtyProcess)
    process.input_fd = write_fd
    try:
        process.signal_eof()
        assert os.read(read_fd, 2) == b"\x1a\r"
        assert process.input_fd == write_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)
