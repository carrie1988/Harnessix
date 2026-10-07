"""以匹配空HEAD对象的真实Index隔离阶段无关入口的debug flags拒绝分支。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_user_observation_verification import (
    _readonly_state,
    _ready,
    _verify,
)
from tests.support import git_user_observation as support


async def test_matching_empty_head_reaches_and_rejects_only_nonzero_debug_flags(
    tmp_path, config, monkeypatch
):
    original_proposal = support._proposal

    def empty_before(root, **kwargs):
        proposal = original_proposal(root, **kwargs)
        first, *remaining = proposal.files
        (root / first.path).write_bytes(b"")
        return WorkspacePatchInput(files=(
            first.model_copy(update={"expected_sha256": hashlib.sha256(b"").hexdigest()}),
            *remaining,
        ))

    monkeypatch.setattr(support, "_proposal", empty_before)

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        member = expected.baseline.members[0]
        mutation = expected.baseline.source.mutations[0]
        assert mutation.before.size == 0
        assert member.oid == command(scenario.root, "hash-object", "--stdin").strip().decode()
        command(scenario.root, "update-index", "--force-remove", "--", member.path)
        command(scenario.root, "add", "--intent-to-add", "--", member.path)
        # stage和-v均匹配原HEAD，不能借前两项提前拒绝掩盖debug flags漏检。
        assert command(scenario.root, "ls-files", "--stage", "-z", "--", member.path) == (
            f"{member.mode} {member.oid} 0\t{member.path}\0".encode()
        )
        assert command(scenario.root, "ls-files", "-v", "-z", "--", member.path) == (
            f"H {member.path}\0".encode()
        )
        assert b"flags: 20004000" in command(
            scenario.root, "ls-files", "--debug", "-z", "--", member.path
        )
        before, calls, original = _readonly_state(scenario), [], scenario.reader._run_baseline

        async def recorded(arguments, cancel, *, repository_check=False):
            calls.append(arguments)
            return await original(arguments, cancel, repository_check=repository_check)

        with monkeypatch.context() as context:
            context.setattr(scenario.reader, "_run_baseline", recorded)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history)
            assert caught.value.code == "git_baseline_index_conflict"
        assert any(args[-5:] == ("ls-files", "--debug", "-z", "--", member.path)
                   for args in calls)
        assert not any("cat-file" in args for args in calls)
        assert _readonly_state(scenario) == before

    await support.run_authenticated_observation(tmp_path, config, monkeypatch, inspect)
