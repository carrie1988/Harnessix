"""产品路径：apply_patch_batch可以写密钥与证书文件，但其内容从不显示（ADR 0172）。

真实Gateway、Agent、SQLite与工作区，两代快照各跑一遍。新建、替换、编辑、删除.key与.pem文件（含后缀像证书的
目录certs.pem/）都在批准后写入；审批Review、模型看到的diff_preview与回滚的Review和结果只有前后摘要，
没有正文的任何一行。读取工具照旧拒绝这些路径。
"""

from __future__ import annotations

import hashlib

from harnessix.agent.models import TurnStatus
from harnessix.delivery.trusted_action_contracts import parse_workspace_action_review
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _approval
from tests.product_config.test_patch_missing_parents import (
    GENERATIONS,
    create,
    product,
    run_patch,
    tree,
)
from tests.product_config.test_product_patch_rollback import approve, result, rollback_step
from tests.tools.test_files import execute

# 示例密钥块按段拼接，避免仓库的Secret扫描把测试数据当成真实私钥。
PEM = "PRIVATE" + " KEY"
SECRET_LINES = ("MIIEvSECRETBODYLINEONE", "QUFBQSECRETBODYLINETWO")
KEY = f"-----BEGIN {PEM}-----\n{SECRET_LINES[0]}\n{SECRET_LINES[1]}\n-----END {PEM}-----\n"
CERT = "-----BEGIN CERTIFICATE-----\nCERTSECRETBODYLINE\n-----END CERTIFICATE-----\n"
NEW_KEY = KEY.replace("LINETWO", "ROTATED2")
SECRETS = (*SECRET_LINES, "CERTSECRETBODYLINE", "ROTATED2", "EDITEDCERT")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def assert_hidden(text: str) -> None:
    """可见文本里没有任何一行正文，也没有PEM封装行。"""
    for secret in (*SECRETS, "BEGIN", "END "):
        assert secret not in text, secret


async def review_text(runtime, artifacts, thread, waiting) -> tuple[str, str]:
    """审批Artifact的原文（read_artifact可分页读到的就是它）与其中的Diff正文。"""
    request = _approval(waiting)
    page = await artifacts.read(
        thread.thread_id, runtime.tools.workspace_scope, request.diff_artifact.artifact_id
    )
    review = parse_workspace_action_review(page.text.encode())
    return page.text, "".join(chunk.text for chunk in review.chunks)


async def approved(runtime, provider, root, artifacts, files):
    thread, waiting = await run_patch(runtime, provider, root, files)
    assert waiting.status is TurnStatus.WAITING_APPROVAL, result(waiting).error
    page, diff = await review_text(runtime, artifacts, thread, waiting)
    completed = await approve(runtime, thread.thread_id, waiting)
    output = result(completed)
    assert output.outcome == "succeeded", output.error
    return thread, _approval(waiting), page, diff, output


@GENERATIONS
async def test_key_and_certificate_files_are_written_with_hidden_content(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, artifacts):
        # 新建：父目录certs.pem/本身带证书后缀，同批还有普通文件。
        _, _, page, diff, output = await approved(
            runtime,
            provider,
            root,
            artifacts,
            [create("certs.pem/server.key", KEY), create("a.pem", CERT), create("ok.txt", "ok\n")],
        )
        assert tree(root)["certs.pem/server.key"] == KEY.encode()
        assert tree(root)["a.pem"] == CERT.encode()
        # 审批Review（含可分页正文）与模型看到的结果都只有摘要；普通文件的Diff照常显示。
        marker = f"Content hidden (key or certificate file): before=absent after={sha(KEY)}"
        assert marker in diff and f"after={sha(CERT)}" in diff and "+ok" in diff
        preview = output.output["diff_preview"]["text"]
        assert marker in preview and "+ok" in preview
        for visible in (page, diff, output.model_dump_json()):
            assert_hidden(visible)

        # 替换与编辑：前后两个版本都不显示。
        edited = CERT.replace("CERTSECRETBODYLINE", "EDITEDCERT")
        thread, request, page, diff, output = await approved(
            runtime,
            provider,
            root,
            artifacts,
            [
                {
                    "operation": "replace",
                    "path": "certs.pem/server.key",
                    "expected_sha256": sha(KEY),
                    "content": NEW_KEY,
                    "mode": 420,
                },
                {
                    "operation": "edit",
                    "path": "a.pem",
                    "expected_sha256": sha(CERT),
                    "edits": [{"old_text": "CERTSECRETBODYLINE", "new_text": "EDITEDCERT"}],
                },
            ],
        )
        assert tree(root)["certs.pem/server.key"] == NEW_KEY.encode()
        assert tree(root)["a.pem"] == edited.encode()
        assert f"before={sha(KEY)} after={sha(NEW_KEY)}" in diff
        assert f"before={sha(CERT)} after={sha(edited)}" in diff
        for visible in (page, diff, output.model_dump_json()):
            assert_hidden(visible)

        # 回滚这次替换与编辑：逆向事务的Review与结果同样只有摘要。
        provider.steps = (rollback_step(request.plan_id), answer("回滚完成"))
        waiting = await runtime.run_turn(thread.thread_id, "撤销", request_id="rollback")
        assert waiting.status is TurnStatus.WAITING_APPROVAL, result(waiting).error
        page, diff = await review_text(runtime, artifacts, thread, waiting)
        assert f"before={sha(NEW_KEY)} after={sha(KEY)}" in diff
        completed = await approve(runtime, thread.thread_id, waiting)
        rolled_back = next(
            item.content
            for item in reversed(completed.items)
            if getattr(item.content, "kind", None) == "tool_result"
        )
        assert rolled_back.outcome == "succeeded", rolled_back.error
        assert tree(root)["certs.pem/server.key"] == KEY.encode()
        assert tree(root)["a.pem"] == CERT.encode()
        for visible in (page, diff, rolled_back.model_dump_json()):
            assert_hidden(visible)

        # 删除。
        _, _, page, diff, output = await approved(
            runtime,
            provider,
            root,
            artifacts,
            [{"operation": "delete", "path": "a.pem", "expected_sha256": sha(CERT)}],
        )
        assert "a.pem" not in tree(root)
        assert f"before={sha(CERT)} after=absent" in diff
        for visible in (page, diff, output.model_dump_json()):
            assert_hidden(visible)


@GENERATIONS
async def test_read_tools_still_deny_key_and_certificate_files(tmp_path, closure):
    async with product(tmp_path, closure=closure) as (root, runtime, provider, artifacts):
        await approved(runtime, provider, root, artifacts, [create("certs/server.key", KEY)])
        read = await execute(runtime.tools, path="certs/server.key")
    assert read.outcome == "failed" and read.error.code == "tool_path_denied"
    # 搜索与列举照旧看不到这个文件（默认读取策略不变）。
    async with CodingToolRuntime(root) as tools:
        found = await execute(tools, "grep", query=SECRET_LINES[0])
        globbed = await execute(tools, "glob", pattern="**/*.key")
        listed = await execute(tools, "list_files", path="certs")
    assert {found.outcome, globbed.outcome, listed.outcome} == {"succeeded"}
    for item in (read, found, globbed, listed):
        assert_hidden(item.model_dump_json())
        assert "server.key" not in item.model_dump_json()


@GENERATIONS
async def test_edit_anchor_failures_on_a_key_file_reveal_no_location(tmp_path, closure):
    """锚点缺失、不唯一与重叠都只给通用说明：没有行号、处数、片段，也不说改动是否已在文件里。"""
    body = "AAA\nREPEATED\nREPEATED\nZZZ\n"
    async with product(tmp_path, closure=closure) as (root, runtime, provider, artifacts):
        await approved(runtime, provider, root, artifacts, [create("dev.key", body)])
        edits_by_case = {
            "patch_context_not_found": [{"old_text": "MISSING", "new_text": "AAA"}],
            "patch_ambiguous_context": [{"old_text": "REPEATED", "new_text": "X"}],
            "patch_overlapping_edits": [
                {"old_text": "AAA\nREPEATED\nREPEATED", "new_text": "X"},
                {"old_text": "REPEATED\nZZZ", "new_text": "Y"},
            ],
        }
        for code, edits in edits_by_case.items():
            item = {"operation": "edit", "path": "dev.key", "expected_sha256": sha(body)}
            _, turn = await run_patch(runtime, provider, root, [{**item, "edits": edits}])
            failed = result(turn)
            assert failed.outcome == "failed" and failed.error.code == code
            message = failed.error.message
            assert "line" not in message and "AAA" not in message and "REPEATED" not in message
            assert "already" not in message
            if code != "patch_overlapping_edits":
                assert "operation replace" in message
        assert tree(root)["dev.key"] == body.encode()
