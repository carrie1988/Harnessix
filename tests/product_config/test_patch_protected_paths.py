"""产品路径：apply_patch_batch写受保护的路径（密钥、证书、凭据目录）只拒绝这次调用。

工作区读取策略按名称与后缀保护.ssh、.env、.key、.pem等路径。规划时观察Workspace以这类路径失败，
此前未转换为可修正的拒绝，整轮以runtime_error中断（2026-11-17 NL2RepoBench冒烟：wsgidav新建
sample_bogo_server.key）。现在与Delivery层的保护路径一样报告delivery_path_denied，整批不写入，
模型据说明改用其他做法，Turn继续。
"""

from __future__ import annotations

import pytest

from harnessix.agent.models import TurnStatus
from tests.product_config.test_patch_missing_parents import (
    GENERATIONS,
    create,
    product,
    run_patch,
    tree,
)
from tests.product_config.test_product_patch_rollback import result

KEY = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"


@GENERATIONS
@pytest.mark.parametrize(
    "files",
    [
        [create("server.key", KEY)],
        [create("src/ok.py", "x = 1\n"), create("certs/server.pem", KEY)],
        [create("bundle.p12", "k\n")],
        [create(".ssh/config", "Host x\n")],
    ],
    ids=["key", "mixed-batch", "p12", "ssh-dir"],
)
async def test_a_protected_path_rejects_only_this_call(tmp_path, closure, files):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, _):
        before = tree(root)
        _, turn = await run_patch(runtime, provider, root, files)

    # Turn照常结束（模型收到拒绝后回复），没有未知效果与中断。
    assert turn.status is TurnStatus.COMPLETED, turn.error
    failed = result(turn)
    assert failed.outcome == "failed" and failed.error is not None
    assert failed.error.code == "delivery_path_denied"
    assert ".key, .pem, .p12 and .pfx" in failed.error.message
    assert "Nothing was written" in failed.error.message
    # 整批原子：同批的其他文件也没有写入。
    assert tree(root) == before
