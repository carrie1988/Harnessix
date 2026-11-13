"""几个Harnessix进程同时改同一份私有记录：每次确认都保留，替换不报错（Beta Issue #15）。

0.10.0b32 Windows：同时在3个新仓库确认信任，1个进程在`os.replace`处
以`PermissionError [WinError 5]`崩溃；另外显示成功的确认也有丢失（4个进程同时写，最后只剩1～2条）。
丢失来自没有串行的“读出→合并→写回”，三个平台都会发生；
替换被拒绝只在Windows上发生（目标文件正被其他进程读取）。
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from harnessix.product_config import private_json
from harnessix.product_config.mcp_trust import load_mcp_trust
from harnessix.product_ui.approval_rules import load_rules
from harnessix.product_ui.workspace_trust import (
    _load,
    confirm_workspace_trust,
)

WRITERS = 4
# 读出之后停这么久再写回：没有串行时几个进程都读到空记录，写回时互相覆盖。
READ_PAUSE_SECONDS = 0.3


def _slow(module: object, name: str) -> None:
    original = getattr(module, name)

    def slowed(*arguments: object) -> object:
        value = original(*arguments)
        time.sleep(READ_PAUSE_SECONDS)
        return value

    setattr(module, name, slowed)


def _write(kind: str, store: str, index: int, barrier) -> None:  # type: ignore[no-untyped-def]
    """子进程：放慢读取后，与其他进程同时写入第index条记录。"""

    if kind == "workspace_trust":
        from harnessix.product_ui import workspace_trust

        _slow(workspace_trust, "_load")
        workspace = Path(store).parent / f"repo-{index}"
        workspace.mkdir(exist_ok=True)
        barrier.wait()
        workspace_trust.remember_trust(workspace, Path(store))
    elif kind == "approval_rules":
        from harnessix.product_ui import approval_rules

        _slow(approval_rules, "load_rules")
        rules = approval_rules.CommandRules(Path(store).parent, Path(store), "posix")
        barrier.wait()
        rules.remember((f"tool-{index}", "run"))
    else:
        from harnessix.product_config import mcp_trust

        _slow(mcp_trust, "load_mcp_trust")
        barrier.wait()
        mcp_trust.trust_mcp_server(Path(store), f"server-{index}", f"{index:064x}")


def _entries(kind: str, store: Path) -> int:
    if kind == "workspace_trust":
        return len(_load(store))
    if kind == "approval_rules":
        return len(load_rules(store, "posix"))
    return len(load_mcp_trust(store))


@pytest.mark.parametrize("kind", ["workspace_trust", "approval_rules", "mcp_trust"])
def test_concurrent_confirmations_are_all_kept(tmp_path: Path, kind: str) -> None:
    store = tmp_path / "home" / f"{kind}.json"
    store.parent.mkdir()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(WRITERS)
    processes = [
        context.Process(target=_write, args=(kind, str(store), index, barrier))
        for index in range(WRITERS)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    assert [process.exitcode for process in processes] == [0] * WRITERS
    assert _entries(kind, store) == WRITERS


def test_windows_retries_a_replace_refused_while_another_process_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows上目标文件正被读取时替换返回WinError 5；读取很快结束，重试后写入成功。"""

    real_replace = private_json.os.replace
    refusals = {"left": 2}

    def busy_replace(source: object, target: object) -> None:
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "拒绝访问。", str(target))
        real_replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(private_json.os, "replace", busy_replace)
    monkeypatch.setattr(private_json, "_RETRY_REPLACE_ON_PERMISSION_ERROR", True)
    store = tmp_path / "records.json"
    private_json.write_private_json(store, {"value": 1})
    assert store.read_text(encoding="utf-8").strip() == '{\n  "value": 1\n}'
    assert refusals["left"] == 0
    assert list(tmp_path.iterdir()) == [store]


def test_posix_reports_a_real_permission_error_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def refused(source: object, target: object) -> None:
        calls.append(target)
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(private_json.os, "replace", refused)
    monkeypatch.setattr(private_json, "_RETRY_REPLACE_ON_PERMISSION_ERROR", False)
    with pytest.raises(PermissionError):
        private_json.write_private_json(tmp_path / "records.json", {"value": 1})
    assert len(calls) == 1


def test_a_stuck_holder_gives_a_clear_busy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_json, "LOCK_WAIT_SECONDS", 0.2)
    store = tmp_path / "records.json"
    with private_json.serialized_update(store):
        with pytest.raises(private_json.PrivateJsonBusy):
            with private_json.serialized_update(store):
                pytest.fail("锁被占用时不应进入")
    # 放开后可以再次取得。
    with private_json.serialized_update(store):
        pass


def test_a_confirmed_workspace_continues_when_the_record_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """用户已经输入y：记录写不进去时本次照常进入，提示下次还会询问，不出现Traceback。"""

    from harnessix.product_ui import workspace_trust

    def unavailable(_workspace: Path, _store: Path) -> None:
        raise private_json.PrivateJsonBusy("另一个Harnessix进程正在更新这份记录")

    monkeypatch.setattr(workspace_trust, "remember_trust", unavailable)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    assert confirm_workspace_trust(
        workspace,
        store=tmp_path / "trusted.json",
        environ={},
        interactive=True,
        read_answer=lambda _prompt: "y",
    )
    assert "信任记录没有保存成功" in capsys.readouterr().err
