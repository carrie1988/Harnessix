"""原九项 Git stderr 固定字节信号；不解码、执行或推断效果。"""

MAX_STDERR_SIGNAL_BYTES = 1024 * 1024
# 字面量依据 worker 固定失败行及 Git v2.53.0、Windows v2.55.0.windows.5 的
# object-file.c / usage.c；英文固定信号不代表 errno、真实根因或效果已知。
_STDERR_LITERALS = {
    "worker_failure_literal": (b"git_material_worker_failed", True),
    "git_temp_create_prefix": (b"error: unable to create temporary file: ", False),
    "git_object_db_permission_prefix": (
        b"error: insufficient permission for adding an object to repository database ",
        False,
    ),
    "git_malformed_object_literal": (b"fatal: refusing to create malformed object", True),
    # Git v2.55.0.windows.5 object-file.c:1083/1086，stdin 的 path 为 NULL，
    # 读错误标签使用 <unknown>；errno 尾部不解码、不提取或保存。
    "READ_ERROR": (b"error: read error while indexing <unknown>: ", False),
    "SHORT_READ": (b"error: short read while indexing <unknown>", True),
    # builtin/hash-object.c:28-33/81/137-138：--stdin 且无 --path 时 vpath=NULL。
    # 仅识别 NULL 的固定 (null) 表示；其他 CRT 表示未匹配，不等于未进入分支。
    "HASH_FD": (b"fatal: Unable to add (null) to database", True),
    # object-file.c:719/594 的 die_errno 固定前缀；阳性只是错误点信号。
    "LOOSE_WRITE": (b"fatal: unable to write loose object file: ", False),
    "LOOSE_CLOSE": (b"fatal: error when closing loose object file: ", False),
}


def _stderr_signals(stderr: bytes) -> dict[str, bool]:
    """只识别完整行的求证字节信号；不解码正文，也不推断 errno、根因或效果。"""
    signals = {name: False for name in _STDERR_LITERALS}
    if len(stderr) > MAX_STDERR_SIGNAL_BYTES:
        return signals
    start = 0
    while (end := stderr.find(b"\n", start)) >= 0:
        for name, (literal, exact) in _STDERR_LITERALS.items():
            stop = start + len(literal)
            if stderr.startswith(literal, start, end):
                complete = end == stop or (end == stop + 1 and stderr.startswith(b"\r", stop))
                signals[name] |= complete if exact else end > stop and not complete
        start = end + 1
    return signals
