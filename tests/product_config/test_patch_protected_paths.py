"""产品路径：apply_patch_batch写受保护的路径（凭据目录、凭据文件名、.env）只拒绝这次调用。

规划时观察Workspace以这类路径失败，此前未转换为可修正的拒绝，整轮以runtime_error中断
（2026-11-17 NL2RepoBench冒烟：wsgidav新建sample_bogo_server.key）。现在与Delivery层的保护路径一样
报告delivery_path_denied，整批不写入，模型据说明改用其他做法，Turn继续。

自ADR 0172起密钥与证书文件（.key、.pem、.p12、.pfx）在所有平台都可以写（见
test_patch_key_certificate_files.py）；.git、.ssh、.aws、.gnupg、.env*与id_rsa等在两个平台上都不能写。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.delivery.trusted_action import workspace_patch_binding
from harnessix.tools.contracts import ReadToolError
from harnessix.trusted_actions.preparation_rejection import workspace_patch_planning_rejection
from tests.product_config.test_patch_missing_parents import (
    GENERATIONS,
    create,
    product,
    run_patch,
    tree,
)
from tests.product_config.test_product_patch_rollback import result

# 示例密钥块按段拼接，避免仓库的Secret扫描把测试数据当成真实私钥。
PEM = "PRIVATE" + " KEY"
KEY = f"-----BEGIN {PEM}-----\nAAAA\n-----END {PEM}-----\n"


@GENERATIONS
@pytest.mark.parametrize(
    "files",
    [
        [create(".ssh/config", "Host x\n")],
        [create("src/ok.py", "x = 1\n"), create(".env", "TOKEN=x\n")],
        [create(".env.local", "TOKEN=x\n")],
        [create("id_rsa", KEY)],
        [create("home/.aws/credentials", "[default]\n")],
        [create("certs/ok.pem", KEY), create(".gnupg/pubring.kbx", "k\n")],
    ],
    ids=["ssh-dir", "mixed-env", "env-variant", "id-rsa", "aws-dir", "mixed-gnupg"],
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
    assert ".git, .env, .ssh or id_rsa" in failed.error.message
    # 密钥与证书文件已经可以写，说明不再把它们列为受保护（ADR 0172）。
    assert ".key" not in failed.error.message and ".pem" not in failed.error.message
    assert "8.3 short name" in failed.error.message
    assert "Nothing was written" in failed.error.message
    # 整批原子：同批的其他文件（包括可写的证书文件）也没有写入。
    assert tree(root) == before


def test_only_the_built_in_patch_turns_a_protected_path_into_a_rejection():
    """转换只用于内置Patch：其他可信操作（命令、Git检查点、回滚）沿原错误处理，不报“Patch”措辞的拒绝；
    其他读取错误也不转换。"""
    call = SimpleNamespace(call_id=uuid4(), arguments={"files": [{"path": ".ssh/config"}]})
    patch = workspace_patch_binding()
    rejected = workspace_patch_planning_rejection(patch, call, ReadToolError("path_denied"))
    assert rejected is not None and rejected.error.code == "delivery_path_denied"
    other = patch.model_copy(update={"tool": "exec_command", "executor_id": "product.process"})
    assert workspace_patch_planning_rejection(other, call, ReadToolError("path_denied")) is None
    assert workspace_patch_planning_rejection(patch, call, ReadToolError("not_found")) is None
