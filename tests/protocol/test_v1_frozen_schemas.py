"""协议升级不能改写已发布合同字节；不以实时模型重新生成旧联合。"""

import hashlib
from pathlib import Path

import pytest

FROZEN = {
    "spec/agent-event-v20.schema.json": (
        "b9b7ab20184832c017f25bd91a67bd90b5f30cbe7f37dd6b91a5891a20cb992c"
    ),
    "spec/agent-thread-v20.schema.json": (
        "23f7de0415044e6eae78d1b264df4551278d65a30d2298a52b62f1c425d3dbe7"
    ),
    "spec/provider-event-v3.schema.json": (
        "af529e302e99bd793737262054235fd5f3c9352fbdbef2f9d138220361bfd0d5"
    ),
    "spec/thread-fork-v1.schema.json": (
        "9e7f154ce8b45369ef4f3ab9900151c0355345ffafab44e85d68b59430e0466c"
    ),
    "spec/agent-protocol-command-params-v1.schema.json": (
        "601a1d507747c6c2e04e4431cd1e1ff1fe873b364798fbeb4c0855edbca0f90f"
    ),
    "spec/agent-protocol-event-v1.schema.json": (
        "3f863bf7b456dd0759227a424b5dd3796092ceef917c7e7e6bd9468ee609265a"
    ),
    "spec/agent-protocol-initialize-params-v1.schema.json": (
        "56499b89ca9e9c9ba82b0c3bdca1435b87dc696649877d25c5ce5fc3f0ea2299"
    ),
    "spec/agent-protocol-initialize-result-v1.schema.json": (
        "7d97fa5a970f63d84d52dc7dea1360a5bf88adf1df779f8ee75802915a25aa91"
    ),
    "spec/agent-protocol-item-v1.schema.json": (
        "3b1abcdcd1f3c2978c7ff683937566c405387f2b346fdeb2a59ba13516df7de8"
    ),
    "spec/agent-protocol-jsonrpc-error-v1.schema.json": (
        "0df291832154e485650c4d0740ad67b72caa6160c11397e5275650beb6eb7eec"
    ),
    "spec/agent-protocol-jsonrpc-notification-v1.schema.json": (
        "f0a8cd8f1e9c6e24d668de8deab03af89b053633139babf339e8665433d274af"
    ),
    "spec/agent-protocol-jsonrpc-request-v1.schema.json": (
        "3a022a10662f53a106c639a1d00d2f07f149e4a36aae663eb422e7ec0350c83f"
    ),
    "spec/agent-protocol-jsonrpc-success-v1.schema.json": (
        "b0f0f8ddd02b2f8ee2a1de60f6db04ea96ca9197d6ec45189ea96aadfa10b109"
    ),
    "spec/agent-protocol-next-result-v1.schema.json": (
        "391be9b1970c78fe605f9cf6bd577ec22c8fb125d8a9cc6c6416fd8de6c3be73"
    ),
    "spec/agent-protocol-query-params-v1.schema.json": (
        "d3b9c5505ab0acbd18a9cc97c0075d7fe850a087280d6022ec230e89918c97be"
    ),
    "spec/agent-protocol-replay-result-v1.schema.json": (
        "7080ac8b22765c627258bea176a4f48c4b40cd92fdc4c20f2c4784aa20bf15b7"
    ),
    "spec/agent-protocol-thread-v1.schema.json": (
        "53af8cde1fd92ad9af742fa3f765b7ae5bcf9ec29c96d9bac2be6dc07c1c5126"
    ),
    "spec/agent-protocol-turn-v1.schema.json": (
        "fe2bfae38ec6c327edf23cad66377fae8367491621b55b35a9c50085b3d74e4b"
    ),
}


@pytest.mark.parametrize("name,digest", FROZEN.items())
def test_previous_published_schemas_keep_original_bytes(name, digest):
    root = Path(__file__).parents[2]
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
