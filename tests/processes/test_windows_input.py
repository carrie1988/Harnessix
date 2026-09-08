from harnessix.processes.windows_conpty import WindowsTtyInputNormalizer


def test_windows_tty_input_normalizes_newlines_backspace_and_chunk_boundary() -> None:
    normalizer = WindowsTtyInputNormalizer()
    assert normalizer.normalize("中\r".encode()) == "中\r".encode()
    assert normalizer.normalize(b"\nnext\nX\x08") == b"next\rX\x7f"
