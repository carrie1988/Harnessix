"""真实共享进程的只读仓库观察；绑定不是原子快照或 GitBridge 业务验收。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from tests.product_config import test_git_material_input as inputs
from tests.product_config.git_repository_observation_support import (
    _UNSAFE_REPOSITORIES,
    _assert_settled,
    _make_unsafe,
    _observe,
    _source_snapshot,
    original,
)
from tests.product_config.git_repository_observation_support import (
    make_process as make_process,
)
from tests.product_config.git_repository_observation_support import (
    shared_repository as shared_repository,
)

pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原生 Process Owner")


async def test_shared_fixture_uses_original_plan_owner_and_keeps_host_alive(shared_repository):
    repository = shared_repository
    case, host = repository.case, repository.host
    before = _source_snapshot(case.workspace)
    host.checkpoint(case.state)
    prepared = case.port.prepare(case.workspace, ("version",), budget=GitOperationBudget(20))
    record = repository.approve(prepared)
    result = await case.port.run(
        prepared,
        record.plan,
        CancelToken(),
        budget=prepared.budget,
        checkpoint=record.approval,
    )
    assert result.stdout.startswith(b"git version ")
    original._assert_completion(case, prepared, result, result.stdout, result.stderr)
    _assert_settled(repository)
    assert _source_snapshot(case.workspace) == before
    await case.port.aclose()
    assert not host.supervisor._closed and not host.plans._closed
    host.checkpoint(case.state)


@pytest.mark.parametrize("shared_repository", ["sha1", "sha256"], indirect=True)
async def test_full_observation_matches_old_binding_and_never_writes_source(
    shared_repository, tmp_path: Path
):
    repository = shared_repository
    case, host = repository.case, repository.host
    expected = inputs._repository_binding_existing(case, tmp_path)
    before = _source_snapshot(case.workspace)
    budget = GitOperationBudget(45)
    actual = await _observe(repository, budget=budget)
    assert actual == expected, {
        key: (expected.model_dump()[key], value)
        for key, value in actual.model_dump().items()
        if expected.model_dump()[key] != value
    }
    assert actual.model_dump_json() == expected.model_dump_json()
    assert (
        len(actual.head_oid)
        == len(actual.head_tree_oid)
        == (40 if actual.object_format == "sha1" else 64)
    )
    assert _source_snapshot(case.workspace) == before
    commands = [record.prepared.command.arguments for record in repository.authorized]
    attributes = case.runner.run(
        case.workspace, ("ls-tree", "-r", "-z", "--full-tree", "HEAD")
    ).stdout
    attribute_oids = [
        record.split(b"\t", 1)[0].split(b" ")[2].decode("ascii")
        for record in attributes.split(b"\0")
        if record and record.split(b"\t", 1)[1].endswith(b".gitattributes")
    ]
    assert commands == [
        ("rev-parse", "--show-toplevel"),
        ("config", "--local", "--no-includes", "--null", "--get-regexp", r"^include(If)?\."),
        ("config", "--includes", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"),
        ("config", "--bool", "--get", "core.sparseCheckout"),
        ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
        *(("cat-file", "blob", oid) for oid in attribute_oids),
        ("rev-parse", "--verify", "HEAD^{commit}"),
        ("rev-parse", "--verify", "HEAD^{tree}"),
        ("status", "--porcelain=v2", "--untracked-files=all", "-z"),
        ("rev-parse", "--git-common-dir"),
        ("config", "--includes", "--null", "--list", "--show-origin"),
        ("version",),
    ]
    assert len(set(record.prepared.spec.process_id for record in repository.authorized)) == len(
        commands
    )
    assert all(record.prepared.budget is budget for record in repository.authorized)
    assert all(record.prepared.command.input_data is None for record in repository.authorized)
    assert all(record.prepared.command.index_file is None for record in repository.authorized)
    assert all(record.prepared.write is None for record in repository.authorized)
    assert repository.checkpoints[0] == 0
    assert set(repository.checkpoints) == set(range(len(commands) + 1))
    _assert_settled(repository)
    await case.port.aclose()
    host.checkpoint(case.state)
    assert not host.supervisor._closed and not host.plans._closed
    # 原宿主继续可用，而非只观察关闭标志仍为假。
    case.port = GitDeliveryProcess(
        case.runner, case.state, output_redaction=host.protection, runtime_host=host
    )
    prepared = case.port.prepare(case.workspace, ("version",), budget=GitOperationBudget(20))
    record = repository.approve(prepared)
    result = await case.port.run(
        prepared,
        record.plan,
        CancelToken(),
        budget=prepared.budget,
        checkpoint=record.approval,
    )
    assert result.stdout.startswith(b"git version ")
    _assert_settled(repository)
    assert _source_snapshot(case.workspace) == before


@pytest.mark.parametrize(("fault", "expected_code"), _UNSAFE_REPOSITORIES)
async def test_unsafe_repositories_keep_original_rejection_and_source_bytes(
    shared_repository, tmp_path: Path, fault: str, expected_code: str
):
    repository = shared_repository
    _make_unsafe(repository, fault, tmp_path)
    before = _source_snapshot(repository.case.workspace)
    with pytest.raises(KernelError) as legacy_failure:
        inputs._repository_binding_existing(repository.case, tmp_path)
    assert legacy_failure.value.code == expected_code
    assert _source_snapshot(repository.case.workspace) == before
    with pytest.raises(KernelError) as failure:
        await _observe(repository)
    assert failure.value.code == legacy_failure.value.code
    assert failure.value.message == legacy_failure.value.message
    _assert_settled(repository)
    assert _source_snapshot(repository.case.workspace) == before
    assert repository.authorized[-1].prepared.command.arguments != ("version",)
