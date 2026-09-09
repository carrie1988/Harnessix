from __future__ import annotations

import json

import pytest

from harnessix.protocol.codec import ProtocolDecodeError, decode_client_frame
from harnessix.protocol.contracts import JsonRpcNotification, JsonRpcRequest


def test_decode_request_and_notification() -> None:
    request = decode_client_frame(
        b'{"jsonrpc":"2.0","id":"rpc-1","method":"thread/get","params":{}}\n'
    )
    notification = decode_client_frame(
        b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\r\n'
    )
    assert isinstance(request, JsonRpcRequest)
    assert isinstance(notification, JsonRpcNotification)


@pytest.mark.parametrize(
    ("frame", "rpc_code", "code"),
    [
        (b"\xff", -32700, "parse_error"),
        (b"{", -32700, "parse_error"),
        (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"x"}', -32700, "parse_error"),
        (b"[]", -32600, "invalid_request"),
        (b"{}", -32600, "invalid_request"),
        (b'{"jsonrpc":"2.0","method":"x"}\n{}', -32600, "invalid_request"),
    ],
)
def test_decode_rejects_invalid_frames(frame: bytes, rpc_code: int, code: str) -> None:
    with pytest.raises(ProtocolDecodeError) as caught:
        decode_client_frame(frame)
    assert (caught.value.rpc_code, caught.value.code) == (rpc_code, code)


def test_decode_enforces_frame_and_depth_limits() -> None:
    with pytest.raises(ProtocolDecodeError, match="长度"):
        decode_client_frame(b"{}", max_message_bytes=1)

    nested: object = None
    for _ in range(65):
        nested = [nested]
    frame = json.dumps({"jsonrpc": "2.0", "method": "x", "params": {"nested": nested}}).encode()
    with pytest.raises(ProtocolDecodeError) as caught:
        decode_client_frame(frame)
    assert caught.value.code == "parse_error"
