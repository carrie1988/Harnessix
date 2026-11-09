"""模型入口的Skill工具：模型只给名称，失败是普通工具结果，不中断Turn。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from harnessix.agent.approvals import tool_fingerprint, trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ToolDescriptor, utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import build_fixed_product_action_environment
from harnessix.product_config.skill_composition import (
    ProductSkills,
    open_product_skills,
    product_skill_sources,
    skill_context_fragments,
)
from harnessix.product_config.skill_diagnostics import list_product_skills
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.skills import (
    SKILL_LOAD_TOOL,
    SKILL_RESOURCE_TOOL,
    SkillCatalogSnapshot,
    SkillRegistry,
    SkillSource,
    SQLiteSkillStore,
)
from harnessix.skills.prompt import MAX_LISTED_SKILLS, MAX_LISTING_BYTES, render_skill_listing
from harnessix.trusted_actions.composite_gateway import CompositeAgentActionGateway
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.agent.helpers import answer


def write_skill(root: Path, directory: str, *, name: str, body: str, description: str = "") -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description or 'Use for ' + name}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@dataclass
class Harness:
    workspace: Path
    skills: ProductSkills
    audit: SQLiteActionAuditStore

    def thread(self) -> tuple[Thread, Turn]:
        now = utc_now()
        turn = Turn(
            turn_id=uuid4(),
            request_id="request",
            request_fingerprint="1" * 64,
            status=TurnStatus.EXECUTING_TOOLS,
            budget=Budget(),
            created_at=now,
        )
        thread = Thread(
            thread_id=uuid4(),
            workspace=str(self.workspace),
            active_turn_id=turn.turn_id,
            turns=(turn,),
            created_at=now,
            updated_at=now,
        )
        return thread, turn

    def descriptor(self, tool: str) -> ToolDescriptor:
        return next(item for item in self.skills.gateway.definitions() if item.name == tool)

    def call(self, tool: str, **arguments: JsonValue) -> ToolCallContent:
        descriptor = self.descriptor(tool)
        return ToolCallContent(
            call_id=uuid4(),
            provider_call_id="provider-call",
            tool=tool,
            tool_version=descriptor.version,
            effect_class=descriptor.effect_class,
            arguments=arguments,
            requires_approval=False,
            tool_fingerprint=tool_fingerprint(descriptor),
        )

    async def run(self, tool: str, **arguments: JsonValue) -> ToolResultContent:
        thread, turn = self.thread()
        result = await self.skills.gateway.prepare(
            thread, turn, self.call(tool, **arguments), CancelToken()
        )
        assert isinstance(result, ToolResultContent)
        return result


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def harness(tmp_path: Path, workspace: Path, *roots: tuple[str, Path]) -> Iterator[Harness]:
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: workspace)
    environment = build_fixed_product_action_environment(workspace)
    sources = tuple(SkillSource(kind, kind, root) for kind, root in roots)  # type: ignore[arg-type]
    with open_product_skills(router, environment, sources) as skills:
        assert skills is not None
        yield Harness(workspace, skills, audit)
    audit.close()
    plans.close()


def output_of(result: ToolResultContent) -> dict[str, object]:
    assert result.outcome == "succeeded", result
    assert isinstance(result.output, dict)
    return cast(dict[str, object], result.output)


def error_of(result: ToolResultContent) -> str:
    assert result.outcome == "failed", result
    assert result.error is not None
    return result.error.code


@pytest.mark.asyncio
async def test_model_loads_body_by_name_without_supplying_digests(
    tmp_path: Path, workspace: Path
) -> None:
    root = tmp_path / "skills"
    skill = write_skill(root, "review", name="review", body="BODY_MARKER_8c1f")
    (skill.parent / "checklist.md").write_text("CHECK_MARKER", encoding="utf-8")
    for item in harness(tmp_path, workspace, ("user", root)):
        properties = item.descriptor(SKILL_LOAD_TOOL).input_schema["properties"]
        assert isinstance(properties, dict) and set(properties) == {"name"}
        assert item.descriptor(SKILL_LOAD_TOOL).requires_approval is False

        loaded = output_of(await item.run(SKILL_LOAD_TOOL, name="review"))
        assert loaded["content"] == "BODY_MARKER_8c1f"
        assert loaded["resources"] == ["checklist.md"]

        resource = output_of(
            await item.run(SKILL_RESOURCE_TOOL, name="review", path="checklist.md")
        )
        assert resource["content"] == "CHECK_MARKER"


@pytest.mark.asyncio
async def test_bad_requests_are_tool_failures_the_model_can_recover_from(
    tmp_path: Path, workspace: Path
) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="body")
    for item in harness(tmp_path, workspace, ("user", root)):
        assert error_of(await item.run(SKILL_LOAD_TOOL, name="missing")) == "skill_not_found"
        assert (
            error_of(await item.run(SKILL_RESOURCE_TOOL, name="review", path="../../etc/passwd"))
            == "skill_resource_path_denied"
        )
        assert (
            error_of(await item.run(SKILL_RESOURCE_TOOL, name="review", path="absent.md"))
            == "skill_resource_not_found"
        )


@pytest.mark.asyncio
async def test_same_name_in_two_roots_requires_the_qualified_name(
    tmp_path: Path, workspace: Path
) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    write_skill(user, "review", name="review", body="USER_BODY")
    write_skill(project, "review", name="review", body="PROJECT_BODY")
    for item in harness(tmp_path, workspace, ("user", user), ("workspace", project)):
        assert error_of(await item.run(SKILL_LOAD_TOOL, name="review")) == "skill_name_conflict"
        loaded = output_of(await item.run(SKILL_LOAD_TOOL, name="workspace/review"))
        assert loaded["content"] == "PROJECT_BODY"

        listing = render_skill_listing(item.skills.catalog)
        assert listing is not None
        assert "- user/review:" in listing and "- workspace/review:" in listing


@pytest.mark.asyncio
async def test_skill_edited_after_discovery_is_refused(tmp_path: Path, workspace: Path) -> None:
    root = tmp_path / "skills"
    path = write_skill(root, "review", name="review", body="ORIGINAL")
    for item in harness(tmp_path, workspace, ("user", root)):
        path.write_text("---\nname: review\ndescription: x\n---\nTAMPERED\n", encoding="utf-8")
        result = await item.run(SKILL_LOAD_TOOL, name="review")
        assert error_of(result) == "skill_content_changed"
        assert "TAMPERED" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_invocation_is_audited_through_the_skill_source(
    tmp_path: Path, workspace: Path
) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="body")
    for item in harness(tmp_path, workspace, ("user", root)):
        thread, turn = item.thread()
        call = item.call(SKILL_LOAD_TOOL, name="review")
        await item.skills.gateway.prepare(thread, turn, call, CancelToken())
        replayed = await item.skills.gateway.recover(thread, turn, call, None, CancelToken())
        assert replayed is not None
        assert replayed.outcome == "succeeded"


@pytest.mark.asyncio
async def test_interrupted_load_settles_without_blocking_the_turn(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="body")
    for item in harness(tmp_path, workspace, ("user", root)):
        thread, turn = item.thread()
        call = item.call(SKILL_LOAD_TOOL, name="review")

        async def crash(_router: TrustedActionRouter, _plan_id: UUID) -> None:
            raise ConnectionError("process lost before execution")

        monkeypatch.setattr(TrustedActionRouter, "execute", crash)
        with pytest.raises(ConnectionError):
            await item.skills.gateway.prepare(thread, turn, call, CancelToken())
        monkeypatch.undo()
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        item.audit.transition(
            plan_id, expected={"ready"}, target="running", executor_id="skill.load"
        )

        recovered = await item.skills.gateway.recover(thread, turn, call, None, CancelToken())

        assert recovered is not None
        assert recovered.outcome != "succeeded"
        assert item.audit.load(plan_id).state == "manual_intervention"


@pytest.mark.asyncio
async def test_agent_runtime_runs_concurrent_skill_calls_through_prepare(
    tmp_path: Path, workspace: Path
) -> None:
    """只读Trusted Action不能走并行读路径，否则会跳过prepare并报approval_mismatch。"""
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="RUNTIME_BODY_A")
    write_skill(root, "style", name="style", body="RUNTIME_BODY_B")
    step: list[ProviderEvent] = [
        ResponseStarted(response_id="skill-response"),
        ToolCallCompleted(call_id="call-a", tool=SKILL_LOAD_TOOL, arguments={"name": "review"}),
        ToolCallCompleted(call_id="call-b", tool=SKILL_LOAD_TOOL, arguments={"name": "style"}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
    for item in harness(tmp_path, workspace, ("user", root)):
        provider = ScriptedProvider([step, answer("done")])
        store = SQLiteSessionStore(tmp_path / "session.db")
        async with AgentRuntime(store, provider, trusted_actions=item.skills.gateway) as runtime:
            thread = await runtime.create_thread(str(workspace))
            turn = await runtime.run_turn(thread.thread_id, "go", request_id="skill")

        assert turn.status is TurnStatus.COMPLETED
        results = [
            entry.content for entry in turn.items if isinstance(entry.content, ToolResultContent)
        ]
        assert [output_of(result)["content"] for result in results] == [
            "RUNTIME_BODY_A",
            "RUNTIME_BODY_B",
        ]


@pytest.mark.asyncio
async def test_composite_routes_by_tool_and_rejects_overlap(
    tmp_path: Path, workspace: Path
) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="COMPOSITE_BODY")
    for item in harness(tmp_path, workspace, ("user", root)):
        with pytest.raises(KernelError) as duplicate:
            CompositeAgentActionGateway(item.skills.gateway, item.skills.gateway)
        assert duplicate.value.code == "trusted_action_gateway_invalid"

        composite = CompositeAgentActionGateway(item.skills.gateway)
        assert {tool.name for tool in composite.definitions()} == {
            SKILL_LOAD_TOOL,
            SKILL_RESOURCE_TOOL,
        }
        thread, turn = item.thread()
        result = await composite.prepare(
            thread, turn, item.call(SKILL_LOAD_TOOL, name="review"), CancelToken()
        )
        assert isinstance(result, ToolResultContent)
        assert output_of(result)["content"] == "COMPOSITE_BODY"

        foreign = item.call(SKILL_LOAD_TOOL, name="review").model_copy(update={"tool": "other"})
        with pytest.raises(KernelError) as unknown:
            await composite.prepare(thread, turn, foreign, CancelToken())
        assert unknown.value.code == "trusted_action_not_registered"


def catalog_for(tmp_path: Path, root: Path) -> SkillCatalogSnapshot:
    with SQLiteSkillStore(tmp_path / "listing/skills.db") as store:
        return SkillRegistry(
            catalog_id="default", sources=(SkillSource("user", "user", root),), store=store
        ).discover()


def test_listing_carries_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="BODY_NOT_LISTED", description="Review code")
    listing = render_skill_listing(catalog_for(tmp_path, root))
    assert listing is not None
    assert "- review: Review code" in listing
    assert "BODY_NOT_LISTED" not in listing


def test_listing_is_bounded_and_reports_what_it_omits(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for index in range(MAX_LISTED_SKILLS + 5):
        write_skill(
            root,
            f"s{index:03}",
            name=f"s{index:03}",
            body="body",
            description="d" * 1000,
        )
    listing = render_skill_listing(catalog_for(tmp_path, root))
    assert listing is not None
    assert len(listing.encode()) <= MAX_LISTING_BYTES + 64
    assert "more installed skills are not listed" in listing


def test_cli_lists_skills_and_explains_rejected_files(tmp_path: Path, workspace: Path) -> None:
    home = tmp_path / "home"
    root = home / ".harnessix/skills"
    write_skill(root, "review", name="review", body="body", description="Review code")
    broken = root / "broken/SKILL.md"
    broken.parent.mkdir()
    broken.write_text("no frontmatter here", encoding="utf-8")

    lines = list_product_skills(workspace, home=home)

    assert any(line.startswith("review  [user]  Review code") for line in lines)
    assert any(
        "未采用：user/broken/SKILL.md（skill_frontmatter_missing）" in line for line in lines
    )


def test_cli_without_roots_says_where_skills_go(tmp_path: Path, workspace: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    lines = list_product_skills(workspace, home=home)
    assert any(str(home / ".harnessix/skills") in line for line in lines)
    assert lines[-1] == "没有可用的 Skill。"
    assert not (home / ".harnessix").exists()


def test_no_root_means_no_sources_and_no_directory_is_created(
    tmp_path: Path, workspace: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert product_skill_sources(workspace, home=home) == ()
    assert not (home / ".harnessix").exists()
    assert not (workspace / ".harnessix").exists()


def test_existing_roots_become_sources_and_symlinked_root_is_ignored(
    tmp_path: Path, workspace: Path
) -> None:
    home = tmp_path / "home"
    (home / ".harnessix/skills").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (workspace / ".harnessix").mkdir()
    (workspace / ".harnessix/skills").symlink_to(target, target_is_directory=True)

    sources = product_skill_sources(workspace, home=home)
    assert [item.source_id for item in sources] == ["user"]


def test_empty_root_adds_no_tools_and_no_context(tmp_path: Path, workspace: Path) -> None:
    empty = tmp_path / "skills"
    empty.mkdir()
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: workspace)
    environment = build_fixed_product_action_environment(workspace)
    with open_product_skills(router, environment, (SkillSource("user", "user", empty),)) as skills:
        assert skills is None
        assert skill_context_fragments(skills) == ()
    assert router.bindings(source="skill") == ()
    audit.close()
    plans.close()


def test_listing_enters_context_below_runtime_instructions(tmp_path: Path, workspace: Path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "review", name="review", body="body", description="Review code")
    for item in harness(tmp_path, workspace, ("user", root)):
        fragments = skill_context_fragments(item.skills)
        assert len(fragments) == 1
        assert fragments[0].kind.value == "project_instruction"
        assert "- review: Review code" in fragments[0].content
