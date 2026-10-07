"""真实原 Store 回调的异常来源；不以错误码猜测是否为上游控制异常。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_git_material_cas import _material


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("kind", ["cancel", "deadline", "callback"])
def test_original_store_callback_exception_identity_is_preserved(tmp_path, phase, kind):
    token = CancelToken()
    token.cancel()
    try:
        token.checkpoint()
    except TurnCancelled as error:
        cancelled = error
    marker = {
        "cancel": cancelled,
        "deadline": KernelError("git_process_timeout", "原期限已耗尽"),
        "callback": ValueError("original checkpoint"),
    }[kind]
    checks = []

    def checkpoint():
        checks.append(True)
        if len(checks) == (1 if phase == "before" else 2):
            raise marker

    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        cas = GitMaterialCAS(store)
        material = _material()
        reference = cas.persist(material)
        store._checkpoint = checkpoint
        with pytest.raises(type(marker)) as caught:
            cas.read(reference)
        assert caught.value is marker
        assert len(checks) == (1 if phase == "before" else 2)
        store._checkpoint = None
        assert cas.read(reference) == material


def test_io_error_with_control_like_code_is_still_material_failure(tmp_path, monkeypatch):
    """材料 IO 主动抛同名错误也不冒充控制来源，区分依据是原控制标记。"""
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        cas = GitMaterialCAS(store)
        ref = cas.persist(_material())

        def damaged(digest):
            raise KernelError("git_process_timeout", "synthetic IO error")

        monkeypatch.setattr(store, "_read_blob", damaged)
        with pytest.raises(KernelError) as caught:
            cas.read(ref)
        assert caught.value.code == "git_material_cas_read_failed"
