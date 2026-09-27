"""按真实宿主选择密钥后端；错误、未知平台和失效均不得降级明文。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from harnessix.product_config.session_key_codec import OwnedSessionKey, decode_payload, unavailable


def load_session_key(root: Path, *, fault: Callable[[str], None] | None = None) -> OwnedSessionKey:
    trigger = fault or (lambda _: None)
    try:
        if os.name == "posix":
            from harnessix.product_config.session_key_posix import load_posix_key

            body = load_posix_key(root, trigger)
        elif os.name == "nt":
            from harnessix.product_config.session_key_windows import load_windows_key

            body = load_windows_key(root, trigger)
        else:
            raise unavailable()
    except OSError:
        raise unavailable() from None
    return decode_payload(body)
