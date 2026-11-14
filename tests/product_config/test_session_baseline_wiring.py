"""产品运行时把会话基线接到自己的Git只读端口上；没有git时同一接线不生效。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.server import run_product_stdio
from harnessix.tools.git_contracts import GitStatusOutput
from tests.product_config.conftest import write_config
from tests.product_config.test_server_and_cli import _credentials
from tests.tools.test_git import _git, _repository


@pytest.mark.parametrize("with_git", [True, False])
async def test_product_runtime_observes_pre_existing_changes_through_its_git_reader(
    tmp_path: Path, config: ProductConfigV2, monkeypatch: pytest.MonkeyPatch, with_git: bool
) -> None:
    from harnessix.product_config import server

    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _repository(workspace)
    (workspace / "tracked.py").write_text("the user's uncommitted work\n", encoding="utf-8")
    options: dict[str, object] = {}
    observed: list[GitStatusOutput | None] = []
    real_runtime = server.AgentRuntime

    def runtime(*args: object, **kwargs: object) -> object:
        options.update(kwargs)
        return real_runtime(*args, **kwargs)  # type: ignore[arg-type]

    async def serve(*_args: object) -> None:
        # 运行时存活期间，会话基线端口就是模型所用Git只读工具背后的同一个运行时。
        observer = options["session_baseline"]
        observed.append(await observer.workspace_changes(CancelToken()))  # type: ignore[attr-defined]

    monkeypatch.setattr(server, "AgentRuntime", runtime)
    monkeypatch.setattr(server, "run_stdio", serve)
    await run_product_stdio(
        config_path=write_config(tmp_path / "config.json", config),
        profile_id=None,
        workspace=workspace,
        state_directory=tmp_path / "state",
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
        git_executable=_git() if with_git else None,
    )

    assert options["session_baseline"] is options["scoped_tools"]
    (status,) = observed
    if with_git:
        assert status is not None
        assert [entry.path for entry in status.entries] == ["tracked.py"]
    else:
        assert status is None
