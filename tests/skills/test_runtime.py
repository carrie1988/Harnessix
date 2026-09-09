from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import SandboxBindingV2, canonical_digest
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.skills import (
    SkillActionGateway,
    SkillLoadInput,
    SkillRegistry,
    SkillResourceReadInput,
    SkillSource,
    SQLiteSkillStore,
    build_skill_action_definitions,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ExtensionActionPort,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str,
    description: str = "安全审查代码",
    version: str | None = "1.0.0",
    body: str = "# 审查\n仅报告可验证问题。",
) -> Path:
    target = root / directory
    target.mkdir(parents=True, exist_ok=True)
    version_line = "" if version is None else f"version: {version}\n"
    document = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{version_line}"
        "allowed-tools: [shell, secret]\n"
        "hooks: {before: dangerous-command}\n"
        "---\n"
        f"{body}\n"
    )
    path = target / "SKILL.md"
    path.write_text(document, encoding="utf-8")
    return path


def registry(tmp_path: Path, *sources: SkillSource) -> tuple[SkillRegistry, SQLiteSkillStore]:
    store = SQLiteSkillStore(tmp_path / "state/skills.db")
    return SkillRegistry(catalog_id="default", sources=tuple(sources), store=store), store


def planning_context(root: Path) -> ActionPlanningContext:
    capabilities = build_capability_evidence_v2(
        platform="windows" if os.name == "nt" else "posix",
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest("skill-test-provider"),
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest("skill-test-profile"),
    )
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=sandbox,
        capabilities=capabilities,
    )


def test_catalog_is_deterministic_and_body_is_progressively_loaded(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    secret_body = "UNIQUE_SKILL_BODY_2dd132b6"
    path = write_skill(source_root, "review", name="review", body=secret_body)
    (path.parent / "references").mkdir()
    (path.parent / "references/checklist.md").write_text("checklist", encoding="utf-8")
    runtime, store = registry(
        tmp_path,
        SkillSource("workspace", "workspace", source_root),
    )

    first = runtime.discover()
    second = runtime.discover()
    skill = first.skills[0]

    assert first.catalog_sha256 == second.catalog_sha256
    assert first.generation == 1 and second.generation == 2
    assert skill.qualified_name == "workspace/review"
    assert skill.effective_version == "1.0.0"
    assert secret_body not in first.model_dump_json()
    assert secret_body.encode() not in (tmp_path / "state/skills.db").read_bytes()

    loaded = runtime.load(
        SkillLoadInput(
            catalog_sha256=first.catalog_sha256,
            name="review",
            expected_manifest_sha256=skill.manifest_sha256,
        )
    )
    assert loaded.content == secret_body
    assert loaded.resources == ("references/checklist.md",)
    assert store.events("default")[0].outcome == "succeeded"
    rows = store._db.execute("SELECT payload FROM skill_access_events").fetchall()  # noqa: SLF001
    assert all(secret_body not in row[0] for row in rows)


def test_cross_source_name_conflict_requires_qualified_name(tmp_path: Path) -> None:
    user = tmp_path / "user"
    workspace = tmp_path / "workspace"
    write_skill(user, "review", name="review", body="user body")
    write_skill(workspace, "review", name="review", body="workspace body")
    runtime, _ = registry(
        tmp_path,
        SkillSource("user", "user", user),
        SkillSource("workspace", "workspace", workspace),
    )
    catalog = runtime.discover()
    assert catalog.conflicts[0].qualified_names == ("user/review", "workspace/review")

    with pytest.raises(KernelError) as caught:
        runtime.resolve(catalog, "review", catalog.skills[0].manifest_sha256)
    assert caught.value.code == "skill_name_conflict"

    workspace_skill = next(item for item in catalog.skills if item.source_id == "workspace")
    loaded = runtime.load(
        SkillLoadInput(
            catalog_sha256=catalog.catalog_sha256,
            name="workspace/review",
            expected_manifest_sha256=workspace_skill.manifest_sha256,
        )
    )
    assert loaded.content == "workspace body"


def test_duplicate_qualified_name_and_invalid_frontmatter_are_not_exposed(tmp_path: Path) -> None:
    root = tmp_path / "source"
    write_skill(root, "one", name="duplicate")
    write_skill(root, "two", name="duplicate")
    bad = root / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", root))

    catalog = runtime.discover()

    assert catalog.skills == ()
    assert sorted(issue.code for issue in catalog.issues) == [
        "skill_duplicate_qualified_name",
        "skill_duplicate_qualified_name",
        "skill_frontmatter_missing",
    ]


def test_content_change_after_catalog_is_rejected_and_audited(tmp_path: Path) -> None:
    root = tmp_path / "source"
    path = write_skill(root, "review", name="review", body="before")
    runtime, store = registry(tmp_path, SkillSource("workspace", "workspace", root))
    catalog = runtime.discover()
    skill = catalog.skills[0]
    path.write_text(path.read_text(encoding="utf-8").replace("before", "after"), encoding="utf-8")

    with pytest.raises(KernelError) as caught:
        runtime.load(
            SkillLoadInput(
                catalog_sha256=catalog.catalog_sha256,
                name="review",
                expected_manifest_sha256=skill.manifest_sha256,
            )
        )
    assert caught.value.code == "skill_content_changed"
    assert store.events("default")[-1].error_code == "skill_content_changed"


def test_resources_reject_sensitive_paths_binary_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "source"
    path = write_skill(root, "review", name="review")
    (path.parent / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (path.parent / "binary.bin").write_bytes(b"\xff\x00")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    symlink_supported = True
    try:
        (path.parent / "escape.txt").symlink_to(outside)
    except OSError:
        symlink_supported = False
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", root))
    catalog = runtime.discover()
    skill = catalog.skills[0]
    loaded = runtime.load(
        SkillLoadInput(
            catalog_sha256=catalog.catalog_sha256,
            name="review",
            expected_manifest_sha256=skill.manifest_sha256,
        )
    )
    assert ".env" not in loaded.resources
    if symlink_supported:
        assert "escape.txt" not in loaded.resources

    with pytest.raises(KernelError) as sensitive:
        runtime.read_resource(
            SkillResourceReadInput(
                catalog_sha256=catalog.catalog_sha256,
                name="review",
                expected_manifest_sha256=skill.manifest_sha256,
                path=".env",
            )
        )
    assert sensitive.value.code == "skill_resource_path_denied"

    with pytest.raises(KernelError) as binary:
        runtime.read_resource(
            SkillResourceReadInput(
                catalog_sha256=catalog.catalog_sha256,
                name="review",
                expected_manifest_sha256=skill.manifest_sha256,
                path="binary.bin",
            )
        )
    assert binary.value.code == "skill_resource_invalid_utf8"


def test_frontmatter_alias_expansion_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "source/large"
    root.mkdir(parents=True)
    aliases = "[" + ",".join("*a" for _ in range(256)) + "]"
    (root / "SKILL.md").write_text(
        "---\nname: large\ndescription: bounded\na: &a [x, y]\nb: " + aliases + "\n---\nbody\n",
        encoding="utf-8",
    )
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", root.parent))
    catalog = runtime.discover()
    assert catalog.skills == ()
    assert catalog.issues[0].code == "skill_frontmatter_limit"


def test_duplicate_yaml_key_is_rejected_instead_of_silently_overridden(tmp_path: Path) -> None:
    root = tmp_path / "source/review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: review\nname: hijacked\ndescription: duplicate\n---\nbody\n",
        encoding="utf-8",
    )
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", root.parent))

    catalog = runtime.discover()

    assert catalog.skills == ()
    assert catalog.issues[0].code == "skill_frontmatter_invalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX硬链接攻击用例")
def test_hard_link_resource_is_listed_but_cannot_be_read(tmp_path: Path) -> None:
    root = tmp_path / "source"
    path = write_skill(root, "review", name="review")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.link(outside, path.parent / "hardlink.txt")
    runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", root))
    catalog = runtime.discover()
    skill = catalog.skills[0]
    loaded = runtime.load(
        SkillLoadInput(
            catalog_sha256=catalog.catalog_sha256,
            name="review",
            expected_manifest_sha256=skill.manifest_sha256,
        )
    )
    assert "hardlink.txt" in loaded.resources

    with pytest.raises(KernelError) as caught:
        runtime.read_resource(
            SkillResourceReadInput(
                catalog_sha256=catalog.catalog_sha256,
                name="review",
                expected_manifest_sha256=skill.manifest_sha256,
                path="hardlink.txt",
            )
        )
    assert caught.value.code == "skill_path_denied"


def test_source_root_symlink_is_rejected_before_discovery(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台未授权创建目录符号链接")

    with pytest.raises(KernelError) as caught:
        SkillSource("workspace", "workspace", link)
    assert caught.value.code == "skill_source_invalid"


def test_store_preserves_equal_catalog_generations_and_detects_catalog_corruption(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    write_skill(root, "review", name="review")
    runtime, store = registry(tmp_path, SkillSource("workspace", "workspace", root))
    first = runtime.discover()
    second = runtime.discover()
    assert first.catalog_sha256 == second.catalog_sha256
    assert store.load_catalog("default", digest=first.catalog_sha256).generation == 2
    store.close()
    database = sqlite3.connect(tmp_path / "state/skills.db")
    database.execute(
        "UPDATE skill_catalogs SET digest = ? WHERE catalog_id = ? AND generation = ?",
        ("f" * 64, "default", 2),
    )
    database.commit()
    database.close()

    corrupt = SQLiteSkillStore(tmp_path / "state/skills.db")
    with pytest.raises(KernelError) as caught:
        corrupt.load_catalog("default", generation=2)
    assert caught.value.code == "skill_store_corrupt"


async def test_skill_body_and_resource_execute_only_through_action_port(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = write_skill(source, "review", name="review")
    (path.parent / "guide.md").write_text("guide", encoding="utf-8")
    skill_runtime, skill_store = registry(
        tmp_path,
        SkillSource("workspace", "workspace", source),
    )
    catalog = skill_runtime.discover()
    skill = catalog.skills[0]
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    for definition in build_skill_action_definitions(skill_runtime, catalog):
        router.register(definition)
    port = ExtensionActionPort(
        router,
        source="skill",
        source_id="default",
        context=lambda: planning_context(workspace),
    )
    gateway = SkillActionGateway(catalog, port)

    load_plan = gateway.plan_load(
        invocation_id=uuid4(),
        name="review",
        expected_manifest_sha256=skill.manifest_sha256,
    )
    loaded = await gateway.execute(load_plan.plan.execution.plan_id)
    resource_plan = gateway.plan_resource(
        invocation_id=uuid4(),
        name="review",
        expected_manifest_sha256=skill.manifest_sha256,
        path="guide.md",
    )
    resource = await gateway.execute(resource_plan.plan.execution.plan_id)

    assert load_plan.state == "ready" and loaded.kind == "succeeded"
    assert loaded.output["content"].startswith("# 审查")  # type: ignore[index]
    assert resource.output["content"] == "guide"  # type: ignore[index]
    assert [event.outcome for event in skill_store.events("default")] == [
        "succeeded",
        "succeeded",
    ]
    assert router.status(load_plan.plan.execution.plan_id).state == "succeeded"
    plans.close()
    audit.close()


async def test_secret_canary_in_skill_output_is_blocked_by_action_boundary(tmp_path: Path) -> None:
    canary = b"SKILL-CANARY-23cf02"
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_skill(source, "review", name="review", body=canary.decode())
    skill_runtime, _ = registry(tmp_path, SkillSource("workspace", "workspace", source))
    catalog = skill_runtime.discover()
    skill = catalog.skills[0]
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    for definition in build_skill_action_definitions(
        skill_runtime,
        catalog,
        protected_secret_values=(canary,),
    ):
        router.register(definition)
    gateway = SkillActionGateway(
        catalog,
        ExtensionActionPort(
            router,
            source="skill",
            source_id="default",
            context=lambda: planning_context(workspace),
        ),
    )
    plan = gateway.plan_load(
        invocation_id=uuid4(),
        name="review",
        expected_manifest_sha256=skill.manifest_sha256,
    )

    outcome = await gateway.execute(plan.plan.execution.plan_id)

    assert outcome.kind == "failed"
    assert outcome.error_code == "secret_redaction_failed"
    assert canary not in (tmp_path / "state/audit.db").read_bytes()
    plans.close()
    audit.close()


def test_skill_store_detects_catalog_and_event_corruption(tmp_path: Path) -> None:
    root = tmp_path / "source"
    write_skill(root, "review", name="review")
    runtime, store = registry(tmp_path, SkillSource("workspace", "workspace", root))
    catalog = runtime.discover()
    skill = catalog.skills[0]
    runtime.load(
        SkillLoadInput(
            catalog_sha256=catalog.catalog_sha256,
            name="review",
            expected_manifest_sha256=skill.manifest_sha256,
        )
    )
    store.close()
    database = sqlite3.connect(tmp_path / "state/skills.db")
    database.execute(
        "UPDATE skill_access_events SET digest = ? WHERE catalog_id = 'default' AND sequence = 1",
        ("f" * 64,),
    )
    database.commit()
    database.close()

    corrupt = SQLiteSkillStore(tmp_path / "state/skills.db")
    with pytest.raises(KernelError) as caught:
        corrupt.events("default")
    assert caught.value.code == "skill_store_corrupt"
