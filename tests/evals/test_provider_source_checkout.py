"""付费前的源码来源；路径替身和真实Git状态，不代表模型或Docker验收。"""

import asyncio
import os
import shutil
import subprocess

import pytest

import harnessix
from harnessix.agent.errors import KernelError
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_suite import build_task_pack_suite_config
from scripts import run_engineering_provider_suite_budgeted as host
from tests.evals.test_provider_verification_budget import PERIOD
from tests.evals.test_provider_verification_host import live_config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="当前验证宿主限定POSIX")


@pytest.fixture
def source_case(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    git = shutil.which("git")
    assert git is not None
    for relative in ["scripts/host.py", "src/harnessix/__init__.py", "tracked.txt"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source fixture\n")

    def run(*args):
        return subprocess.check_output(
            [git, "-C", str(root), *args], stderr=subprocess.DEVNULL, timeout=15
        )

    run("init", "-q")
    run("add", ".")
    run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    config = live_config(tmp_path)
    suite = build_task_pack_suite_config(
        builtin_coding_eval_task_pack(config.pack_id, config.pack_version),
        suite_id=config.suite.plan.suite_id,
        work_root=tmp_path / "private-suite",
        environment=config.suite.plan.environment.model_copy(
            update={"harnessix_revision": run("rev-parse", "HEAD").decode().strip()}
        ),
        price=config.suite.campaign_plans[0].price,
        billing_context=config.suite.campaign_plans[0].billing_context,
        fee_stop_amount="40",
        created_at=config.suite.plan.created_at,
    )
    config = config.model_copy(
        update={
            "source_root": str(root),
            "git_executable": git,
            "container_engine": shutil.which("true"),
            "suite": suite,
        }
    )
    # 必须重新构建Case/Campaign身份，不能只替换Suite的Revision后误测配置拒绝。
    host.CodingEvalProviderSuiteRunConfig.model_validate_json(config.model_dump_json(), strict=True)
    monkeypatch.setattr(host, "__file__", str(root / "scripts/host.py"))
    monkeypatch.setattr(harnessix, "__file__", str(root / "src/harnessix/__init__.py"))
    return config, root, run


def forbidden(*args, **kwargs):
    pytest.fail("源码拒绝前不得检查镜像、进入费用Owner、读取凭据或发出请求")


@pytest.mark.parametrize(
    "drift", ["modified", "staged", "untracked", "package_origin", "declared_origin"]
)
async def test_source_drift_refused_before_images_budget_or_credentials(
    source_case, monkeypatch, drift
):
    config, root, run = source_case
    if drift in {"modified", "staged"}:
        (root / "tracked.txt").write_text("changed source\n")
        if drift == "staged":
            run("add", "tracked.txt")
    elif drift == "untracked":
        (root / "untracked.txt").write_text("untracked source\n")
    elif drift == "package_origin":
        other = root.parent / "other.py"
        other.write_text("foreign source fixture\n")
        monkeypatch.setattr(harnessix, "__file__", str(other))
    else:
        other = root.parent / "other-checkout"
        await asyncio.to_thread(
            subprocess.run,
            [config.git_executable, "clone", "-q", str(root), str(other)],
            check=True,
            timeout=15,
        )
        config = config.model_copy(update={"source_root": str(other)})
    monkeypatch.setattr(host, "_require_images", forbidden)
    monkeypatch.setattr(host, "VerificationBudgetLedger", forbidden)
    monkeypatch.setattr(host, "_credential", forbidden)
    with pytest.raises(KernelError) as caught:
        await host.run_budgeted_suite(
            config,
            budget_path=root.parent / "never-created.json",
            period_id=PERIOD,
            allow_network=True,
        )
    assert caught.value.code == "verification_source_checkout_unavailable"
    assert not (root.parent / "never-created.json").exists()


async def test_clean_source_admission_reaches_original_next_gate(source_case, monkeypatch):
    config, root, _ = source_case
    expected = KernelError("verification_image_unavailable", "fixture")

    def next_gate(_):
        raise expected

    monkeypatch.setattr(host, "_require_images", next_gate)
    monkeypatch.setattr(host, "VerificationBudgetLedger", forbidden)
    with pytest.raises(KernelError) as caught:
        await host.run_budgeted_suite(
            config, budget_path=root.parent / "none", period_id=PERIOD, allow_network=True
        )
    assert caught.value is expected
