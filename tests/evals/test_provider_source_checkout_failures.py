"""源码准入失败的短负控；仅使用自有Git夹具，不执行付费或容器操作。"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals import provider_suite_execution
from scripts import run_engineering_provider_suite_budgeted as host
from tests.evals.test_provider_source_checkout import PERIOD, forbidden
from tests.evals.test_provider_source_checkout import source_case as source_case

pytestmark = pytest.mark.skipif(os.name != "posix", reason="当前验证宿主限定POSIX")


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "os_error"])
async def test_status_failure_safely_refused_before_paid_gates(
    source_case, monkeypatch, capsys, failure
):
    config, root, _ = source_case
    original_run = subprocess.run
    status_calls = []
    private_detail = f"PRIVATE_STATUS_SENTINEL {root}"

    def fail_status(command, **kwargs):
        assert Path(kwargs["cwd"]).resolve() == root.resolve()
        assert str(command[0]) == config.git_executable
        if "status" not in command:
            assert "rev-parse" in command
            return original_run(command, **kwargs)
        status_calls.append(tuple(command))
        assert "--porcelain=v1" in command
        assert "--untracked-files=all" in command
        assert kwargs["timeout"] == 30
        assert kwargs["env"] == provider_suite_execution._GIT_ENVIRONMENT
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=private_detail.encode())
        if failure == "os_error":
            raise OSError(private_detail)
        return subprocess.CompletedProcess(command, 128, private_detail.encode())

    monkeypatch.setattr(host.subprocess, "run", fail_status)
    for name in ("_require_images", "VerificationBudgetLedger", "_credential"):
        monkeypatch.setattr(host, name, forbidden)
    budget = root.parent / "never-created.json"
    with pytest.raises(KernelError) as caught:
        await host.run_budgeted_suite(
            config, budget_path=budget, period_id=PERIOD, allow_network=True
        )
    assert caught.value.code == "verification_source_checkout_unavailable"
    assert str(root) not in str(caught.value)
    assert "PRIVATE_STATUS_SENTINEL" not in str(caught.value)
    assert len(status_calls) == 1
    assert not budget.exists()
    output = capsys.readouterr()
    assert output.out == output.err == ""


async def test_original_head_refusal_precedes_new_checkout_check(source_case, monkeypatch):
    config, root, run = source_case
    run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "new fixture revision",
    )
    for name in (
        "_require_source_checkout",
        "_require_images",
        "VerificationBudgetLedger",
        "_credential",
    ):
        monkeypatch.setattr(host, name, forbidden)
    budget = root.parent / "never-created.json"
    with pytest.raises(KernelError) as caught:
        await host.run_budgeted_suite(
            config, budget_path=budget, period_id=PERIOD, allow_network=True
        )
    assert caught.value.code == "eval_provider_suite_source_revision_mismatch"
    assert not budget.exists()


def test_cli_publishes_only_checkout_fixed_code(source_case, monkeypatch, capsys):
    config, root, _ = source_case

    def unavailable(_):
        raise KernelError(
            "verification_source_checkout_unavailable", f"PRIVATE_MESSAGE_SENTINEL {root}"
        )

    monkeypatch.setattr(host, "read_private_eval_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(host, "_require_source_checkout", unavailable)
    for name in ("_require_images", "VerificationBudgetLedger", "_credential"):
        monkeypatch.setattr(host, name, forbidden)
    budget = root.parent / "never-created.json"
    with pytest.raises(SystemExit) as caught:
        host.main(
            [
                "--config",
                str(root / "private-config.json"),
                "--budget-ledger",
                str(budget),
                "--period-id",
                str(PERIOD),
                "--allow-network",
            ]
        )
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reason": "verification_source_checkout_unavailable"}
    assert str(root) not in output.out
    assert "PRIVATE_MESSAGE_SENTINEL" not in output.out
    assert output.err == ""
    assert not budget.exists()
