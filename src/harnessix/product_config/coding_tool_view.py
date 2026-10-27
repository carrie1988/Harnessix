"""Derive a compact coding-tool catalog from durable turn facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from harnessix.agent.errors import AgentFailure
from harnessix.agent.models import (
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    Turn,
)
from harnessix.agent.planning import UPDATE_PLAN_TOOL
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.turn_budget_hint import affordable_request_hint
from harnessix.delivery.rollback_action import WORKSPACE_ROLLBACK_TOOL
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass, ToolDescriptor
from harnessix.product_config.coding_closure_budget import prefer_closing_over_investigation
from harnessix.product_config.coding_reproduction_gate import (
    LOCKED_NOTICE,
    REPRODUCTION_PASSED_NOTICE,
    RUN_REPRODUCTION_NOTICE,
    VERBATIM_SELF_CHECK,
    ReproductionGateState,
    reproduction_admission_failure,
    reproduction_gate_state,
    without_reproduction_rejections,
)
from harnessix.product_config.coding_scratch import is_scratch_path
from harnessix.product_config.coding_test_outcomes import (
    with_test_timeouts_as_failures,
    without_rejected_profile_arguments,
)

_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_GIT_REVIEW_TOOLS = frozenset({"git_status", "git_diff"})
_GIT_REVIEW_TOOL = "git_review"
_ALL_GIT_REVIEW_TOOLS = _GIT_REVIEW_TOOLS | {_GIT_REVIEW_TOOL}
_SOURCE_INSPECTION_TOOLS = frozenset({"read_file", "grep", "glob"})
_HIDDEN_WORKFLOW_ACTIONS = frozenset({WORKSPACE_ROLLBACK_TOOL})
_MAX_NO_PROFILE_INSPECTIONS = 4


@dataclass(frozen=True, slots=True)
class _Observation:
    index: int
    call: ToolCallContent
    result: ToolResultContent

    @property
    def tool(self) -> str:
        return self.call.tool


def _observations(turn: Turn) -> tuple[_Observation, ...]:
    calls: dict[object, ToolCallContent] = {}
    observations: list[_Observation] = []
    for index, item in enumerate(turn.items):
        if item.status != ItemStatus.COMPLETED:
            continue
        if isinstance(item.content, ToolCallContent):
            calls[item.content.call_id] = item.content
        elif isinstance(item.content, ToolResultContent):
            call = calls.get(item.content.call_id)
            if call is not None:
                observations.append(_Observation(index, call, item.content))
    return tuple(observations)


def _diagnostic_is_complete(result: ToolResultContent) -> bool:
    output = result.output
    if not isinstance(output, dict):
        return False
    preview = output.get("diagnostic_preview")
    if not isinstance(preview, dict):
        return False
    streams = (preview.get("stdout"), preview.get("stderr"))
    return all(isinstance(stream, dict) and stream.get("truncated") is False for stream in streams)


def _successful_reads_after(observations: tuple[_Observation, ...], index: int) -> frozenset[str]:
    paths: set[str] = set()
    for observation in observations:
        if (
            observation.index <= index
            or observation.tool != "read_file"
            or observation.result.outcome != "succeeded"
            or not isinstance(observation.result.output, dict)
        ):
            continue
        path = observation.result.output.get("path")
        if isinstance(path, str):
            paths.add(path)
    return frozenset(paths)


def _patched_paths_requiring_reread(
    observations: tuple[_Observation, ...],
    patch_index: int,
    *,
    include_created: bool = True,
) -> frozenset[str]:
    observation = next(item for item in observations if item.index == patch_index)
    arguments = observation.call.arguments
    if observation.tool == "apply_patch":
        path = arguments.get("path")
        return frozenset({path}) if isinstance(path, str) else frozenset()
    files = arguments.get("files")
    if not isinstance(files, list):
        return frozenset()
    return frozenset(
        path
        for item in files
        if isinstance(item, dict)
        and item.get("operation") != "delete"
        and (include_created or item.get("operation") != "create")
        and isinstance((path := item.get("path")), str)
    )


def _selection(
    definitions: tuple[ToolDescriptor, ...],
    names: set[str],
    *,
    forced_tool: str | None = None,
) -> ModelToolSelection:
    tools = tuple(item for item in definitions if item.name in names)
    return ModelToolSelection(
        tools,
        "required" if tools else "auto",
        forced_tool,
    )


def _reread_notice(patch: str, unread: frozenset[str]) -> str:
    paths = ", ".join(sorted(unread))
    if len(paths) > 600:
        paths = paths[:600] + "..."
    return (
        f"{patch} is callable again only after read_file of each file the last patch "
        f"changed: {paths}. Re-read them first; then patch with the fresh content_sha256."
    )


_NO_TOOLS_NOTICE = (
    "No tool is callable in this request. Reply in text with your final answer, "
    "or state the limitation that prevents finishing."
)


def _is_code_test_failure(result: ToolResultContent) -> bool:
    return (
        result.outcome == "failed"
        and result.error is not None
        and result.error.code == "process_nonzero_exit"
    )


def _is_infrastructure_failure(result: ToolResultContent) -> bool:
    return result.outcome in {"cancelled", "unknown"} or (
        result.error is not None
        and result.error.code.startswith("process_")
        and result.error.code != "process_nonzero_exit"
    )


def _failed_patch_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    failed_patch: _Observation,
) -> ModelToolSelection:
    if (
        _is_infrastructure_failure(failed_patch.result)
        or failed_patch.result.outcome != "failed"
        or failed_patch.result.error is None
    ):
        return ModelToolSelection(())
    patch = failed_patch.tool
    patched_paths = _patched_paths_requiring_reread(
        observations,
        failed_patch.index,
        include_created=False,
    )
    if (
        failed_patch.result.error.code == "tool_invalid_arguments"
        or patched_paths <= _successful_reads_after(observations, failed_patch.index)
    ):
        return _selection(definitions, {patch}, forced_tool=patch)
    unread = patched_paths - _successful_reads_after(observations, failed_patch.index)
    return replace(
        _selection(definitions, {"read_file"}, forced_tool="read_file"),
        notice=_reread_notice(patch, unread),
    )


def _pre_patch_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    patch: str,
    patch_results: tuple[_Observation, ...],
    *,
    bound_no_profile_reads: bool = True,
) -> ModelToolSelection:
    if patch_results:
        return _failed_patch_selection(definitions, observations, patch_results[-1])
    source_inspections = tuple(
        item for item in observations if item.tool in _SOURCE_INSPECTION_TOOLS
    )
    successful_inspections = sum(item.result.outcome == "succeeded" for item in source_inspections)
    if bound_no_profile_reads and successful_inspections >= _MAX_NO_PROFILE_INSPECTIONS:
        return _selection(definitions, {patch}, forced_tool=patch)
    tools = tuple(item for item in definitions if item.name in {*_SOURCE_INSPECTION_TOOLS, patch})
    # 有验证Profile的调查阶段允许诚实结束；工具成功次数不代表已找到安全修改依据。
    if not bound_no_profile_reads or any(
        item.result.outcome != "succeeded" for item in source_inspections
    ):
        return ModelToolSelection(tools)
    return ModelToolSelection(tools, "required")


def _git_review_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    after_index: int,
) -> ModelToolSelection:
    if any(item.name == _GIT_REVIEW_TOOL for item in definitions):
        aggregate_observed = any(
            item.index > after_index and item.tool == _GIT_REVIEW_TOOL for item in observations
        )
        return (
            ModelToolSelection(())
            if aggregate_observed
            else _selection(definitions, {_GIT_REVIEW_TOOL}, forced_tool=_GIT_REVIEW_TOOL)
        )
    reviewed = {
        item.tool
        for item in observations
        if item.index > after_index
        and item.tool in _GIT_REVIEW_TOOLS
        and item.result.outcome == "succeeded"
    }
    missing_review = set(_GIT_REVIEW_TOOLS) - reviewed
    forced_review = None
    if len(missing_review) == 1:
        forced_review = next(iter(missing_review))
    return _selection(definitions, missing_review, forced_tool=forced_review)


def _workflow_is_supported(
    definitions: tuple[ToolDescriptor, ...],
    profiles: tuple[str, ...],
    patches: tuple[str, ...],
) -> bool:
    """原单Profile契约；无法收敛的目录沿既有完整目录回退。"""
    names = {definition.name for definition in definitions}
    if len(profiles) > 1 or len(patches) != 1:
        return False
    if not (_GIT_REVIEW_TOOL in names or _GIT_REVIEW_TOOLS <= names):
        return False
    allowed_writes = {*profiles, *patches, *_HIDDEN_WORKFLOW_ACTIONS}
    return not any(
        definition.effect_class != EffectClass.READ_ONLY and definition.name not in allowed_writes
        for definition in definitions
    )


def _complete_content_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _has_edit_snapshot(observations: tuple[_Observation, ...]) -> bool:
    """只检查已发布完整内容SHA；这是调度就绪信号，不替代Patch端的新鲜度检查。"""
    for observation in observations:
        if observation.result.outcome != "succeeded" or not isinstance(
            observation.result.output, dict
        ):
            continue
        output = observation.result.output
        if observation.tool == "read_file" and output.get("digest_status") == "complete":
            if _complete_content_sha(output.get("content_sha256")):
                return True
        if observation.tool == "grep":
            preview = output.get("preview", output)
            matches = preview.get("matches") if isinstance(preview, dict) else None
            if isinstance(matches, list):
                if any(
                    isinstance(hit, dict) and _complete_content_sha(hit.get("content_sha256"))
                    for hit in matches
                ):
                    return True
    return False


def _enforce_patch_deadline(
    turn: Turn,
    selection: ModelToolSelection,
    definitions: tuple[ToolDescriptor, ...],
    deadline: int,
) -> ModelToolSelection:
    """剩余预算只够修改、测试与Review时，结束首次修改前的自由调查并强制提交Patch。

    仅在已有本轮完整内容SHA观察、尚无任何Patch且未处于强制或测试阶段时生效；
    Patch失败后由原修补流程重新开放读取，测试、Review与执行层校验均不改变。
    """
    names = {tool.name for tool in selection.tools}
    patches = names & _PATCH_TOOLS
    observations = _observations(turn)
    affordable = affordable_request_hint(turn)
    if (
        selection.forced_tool is not None
        or selection.choice != "auto"
        or len(patches) != 1
        or any(name.startswith("run_profile.") for name in names)
        or any(_delivers_change(item) for item in observations)
        or not _has_edit_snapshot(observations)
        or affordable is None
        or affordable > deadline
    ):
        return selection
    (patch,) = patches
    return _selection(definitions, {patch}, forced_tool=patch)


def _with_reads_after_failed_patch(
    turn: Turn, selection: ModelToolSelection, definitions: tuple[ToolDescriptor, ...]
) -> ModelToolSelection:
    """仅当最近一次Patch失败且当前只强制Patch时，补开read_file（仍须调用工具，但不再强制Patch）。"""
    forced = selection.forced_tool
    if forced not in _PATCH_TOOLS or not any(d.name == "read_file" for d in definitions):
        return selection
    patches = [item for item in _observations(turn) if item.tool in _PATCH_TOOLS]
    if not patches or patches[-1].result.outcome != "failed":
        return selection
    return replace(
        _selection(definitions, {forced, "read_file"}),
        notice=(
            f"The last {forced} call failed. Re-read the target files with read_file if you need "
            f"their current content_sha256 or exact anchor text, then retry {forced}."
        ),
    )


def _delivers_change(observation: _Observation) -> bool:
    """Patch调用是否触及可交付文件；复现草稿被剔除后为空的调用不算首次修改。"""
    if observation.tool not in _PATCH_TOOLS:
        return False
    files = observation.call.arguments.get("files")
    return bool(files) if isinstance(files, list) else True


def _without_scratch_edits(turn: Turn) -> Turn:
    """视图专用投影：Patch调用只保留非复现草稿文件，草稿不触发重读门禁或首次修改判断。

    失败且只改草稿的调用无任何效果，连同结果整体移出视图；否则它以空路径集
    退化为强制重试Patch，模型无法再读取草稿取得正确SHA。
    """
    results = {
        item.content.call_id: item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
    }
    items = []
    dropped: set[UUID] = set()
    for item in turn.items:
        content = item.content
        if isinstance(content, ToolResultContent) and content.call_id in dropped:
            continue
        files = (
            content.arguments.get("files")
            if isinstance(content, ToolCallContent) and content.tool in _PATCH_TOOLS
            else None
        )
        if isinstance(files, list):
            kept = [
                entry
                for entry in files
                if not (
                    isinstance(entry, dict)
                    and isinstance(entry.get("path"), str)
                    and is_scratch_path(entry["path"])
                )
            ]
            result = results.get(content.call_id)
            if files and not kept and result is not None and result.outcome == "failed":
                dropped.add(content.call_id)
                continue
            if len(kept) != len(files):
                content = content.model_copy(
                    update={"arguments": {**content.arguments, "files": kept}}
                )
                item = item.model_copy(update={"content": content})
        items.append(item)
    return turn.model_copy(update={"items": tuple(items)})


def _workflow_failed(observations: tuple[_Observation, ...]) -> bool:
    """只接受确定的测试非零退出；基础设施或未闭合Profile不能进入修复。"""
    return any(_is_infrastructure_failure(item.result) for item in observations) or any(
        item.tool.startswith("run_profile.")
        and item.result.outcome != "succeeded"
        and not _is_code_test_failure(item.result)
        for item in observations
    )


def _with_exclusive_profile_patch(selection: ModelToolSelection) -> ModelToolSelection:
    """同响应不得同时测试和修改；只在两个工具均合法可见时附加宿主约束。"""
    profiles = tuple(t.name for t in selection.tools if t.name.startswith("run_profile."))
    patches = tuple(t.name for t in selection.tools if t.name in _PATCH_TOOLS)
    if len(profiles) == len(patches) == 1:
        return replace(selection, exclusive_tool_groups=((profiles[0], patches[0]),))
    return selection


def _latest_review_is_clean(
    observations: tuple[_Observation, ...], *, ignore_scratch: bool = False
) -> bool:
    """最近一次成功的git_review在最近一次成功Patch之后，且无可交付改动（可忽略复现草稿）。"""
    patches = [
        o.index for o in observations if o.tool in _PATCH_TOOLS and o.result.outcome == "succeeded"
    ]
    reviews = [
        o for o in observations if o.tool == _GIT_REVIEW_TOOL and o.result.outcome == "succeeded"
    ]
    if not patches or not reviews or reviews[-1].index < patches[-1]:
        return False
    output = reviews[-1].result.output
    status = output.get("status") if isinstance(output, dict) else None
    diff = output.get("diff") if isinstance(output, dict) else None
    if not isinstance(status, dict) or not isinstance(diff, dict) or status.get("truncated"):
        return False
    entries = status.get("entries")
    if not isinstance(entries, list) or len(entries) != status.get("total_entries"):
        return False
    deliverable = [
        entry
        for entry in entries
        if not (
            ignore_scratch
            and isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and is_scratch_path(entry["path"])
        )
    ]
    return not deliverable and diff.get("utf8_bytes") == 0


_VERIFICATION_NOTICE = (
    "Self-check before finishing: re-read the issue and your diff; list every behaviour the "
    "issue requires, including edge cases and the exact output or error it describes; extend "
    "the scratch test to cover them and run it together with the existing test files of each "
    "module you changed. If anything fails or is missing, fix it (the usual test and review "
    "steps follow). If everything is covered, reply with a short final summary."
)


_REPAIR_EXIT_STREAK = 4
_REPAIR_FORCE_STREAK = 8
_REPAIR_FORCE_NOTICE = (
    "The public tests have failed {streak} times in a row after your patches; further edits are "
    "closed. Call git_review now to finish with the current diff."
)
_REPAIR_EXIT_NOTICE = (
    "The public tests have failed {streak} times in a row after your patches. Compare the "
    "failures with the issue: failures that also occur without your change, or assertions that "
    "encode the old behaviour the issue asks to change, need no further edits. If your diff "
    "already implements the issue, call git_review to finish; otherwise fix the remaining "
    "cause shown in the failure output."
)
_VALIDATE_PATCH_NOTICE = (
    "Your patch was applied. Run {profile} now to validate it; it is the only callable tool "
    "in this request."
)


def _failed_validation_streak(observations: tuple[_Observation, ...]) -> int:
    """最近连续的代码级测试失败次数（遇到一次成功验证即中断）。"""
    streak = 0
    for item in reversed(observations):
        if not item.tool.startswith("run_profile."):
            continue
        if not _is_code_test_failure(item.result):
            break
        streak += 1
    return streak


def _with_bounded_repair_cycles(
    turn: Turn,
    selection: ModelToolSelection,
    definitions: tuple[ToolDescriptor, ...],
    *,
    force_review: bool = False,
) -> ModelToolSelection:
    """修复循环有界：连续多次测试失败后开放Review收尾，模型可判定剩余失败与本改动无关。

    只在最近一次验证晚于最近一次成功Patch且为代码级失败时生效；收尾后的Review照旧
    关闭目录。修改后强制测试的请求附带说明，避免模型调用当前不可用的工具。
    """
    observations = _observations(turn)
    patches = [
        item.index
        for item in observations
        if item.tool in _PATCH_TOOLS and item.result.outcome == "succeeded"
    ]
    profiles = [item for item in observations if item.tool.startswith("run_profile.")]
    forced = selection.forced_tool
    if forced is not None and forced.startswith("run_profile.") and patches:
        if not profiles or profiles[-1].index < patches[-1]:
            return replace(selection, notice=_VALIDATE_PATCH_NOTICE.format(profile=forced))
    if not patches or not profiles or profiles[-1].index < patches[-1]:
        return selection
    latest = profiles[-1]
    streak = _failed_validation_streak(observations)
    if streak < _REPAIR_EXIT_STREAK:
        return selection
    if any(
        item.index > latest.index and item.tool in _ALL_GIT_REVIEW_TOOLS for item in observations
    ):
        return ModelToolSelection(())
    review = (
        {_GIT_REVIEW_TOOL}
        if any(item.name == _GIT_REVIEW_TOOL for item in definitions)
        else set(_GIT_REVIEW_TOOLS)
    )
    if force_review and streak >= _REPAIR_FORCE_STREAK:
        forced = _GIT_REVIEW_TOOL if review == {_GIT_REVIEW_TOOL} else None
        return replace(
            _selection(definitions, review, forced_tool=forced),
            notice=_REPAIR_FORCE_NOTICE.format(streak=streak),
        )
    names = {tool.name for tool in selection.tools} | review
    return replace(_selection(definitions, names), notice=_REPAIR_EXIT_NOTICE.format(streak=streak))


_SIBLING_SCOPE_CHECK = (
    " Also search for other implementations of the same behaviour (sibling classes or "
    "subclasses, other backends, printers or formats, duplicated helpers) and apply the fix "
    "consistently wherever the issue's behaviour applies."
)


def _first_closure_with_changes(
    observations: tuple[_Observation, ...], *, ignore_scratch: bool
) -> bool:
    """恰有一次成功的git_review，且它在最近一次成功Patch之后并显示可交付改动。"""
    reviews = [
        o for o in observations if o.tool == _GIT_REVIEW_TOOL and o.result.outcome == "succeeded"
    ]
    return (
        len(reviews) == 1
        and not _latest_review_is_clean(observations, ignore_scratch=ignore_scratch)
        and any(o.tool in _PATCH_TOOLS and o.result.outcome == "succeeded" for o in observations)
    )


def _verification_selection(
    definitions: tuple[ToolDescriptor, ...],
    *,
    sibling_scope: bool = False,
    verbatim_example: bool = False,
) -> ModelToolSelection:
    """自我校验轮：开放调查、修改与测试（auto，可直接总结收尾）；Review门禁随新改动照旧生效。"""
    hidden = _ALL_GIT_REVIEW_TOOLS | _HIDDEN_WORKFLOW_ACTIONS | {"read_artifact"}
    notice = (
        _VERIFICATION_NOTICE
        + (_SIBLING_SCOPE_CHECK if sibling_scope else "")
        + (VERBATIM_SELF_CHECK if verbatim_example else "")
    )
    return ModelToolSelection(
        tuple(tool for tool in definitions if tool.name not in hidden),
        notice=notice,
    )


_CLEAN_REVIEW_NOTICE = (
    "git_review shows no deliverable change: the workspace now equals the original code, so a "
    "later patch undid your fix and nothing would be submitted. Review tools stay closed until "
    "a new patch; re-read the target file, re-apply the intended fix with {patch}, then run "
    "the tests."
)


def _with_reproduction_lock(
    gate: ReproductionGateState,
    selection: ModelToolSelection,
    definitions: tuple[ToolDescriptor, ...],
) -> ModelToolSelection:
    """门禁锁定时不进入Review（仅改了复现草稿），并说明解锁条件；关闭的目录保持关闭。"""
    if not gate.locked or not selection.tools:
        return selection
    forced = selection.forced_tool
    if forced is not None and forced.startswith("run_profile."):
        return replace(selection, notice=RUN_REPRODUCTION_NOTICE)
    names = {tool.name for tool in selection.tools}
    if names & _ALL_GIT_REVIEW_TOOLS or gate.latest_reproduction_passed:
        editable = _SOURCE_INSPECTION_TOOLS | ({tool.name for tool in definitions} & _PATCH_TOOLS)
        return ModelToolSelection(
            tuple(tool for tool in definitions if tool.name in editable),
            notice=REPRODUCTION_PASSED_NOTICE if gate.latest_reproduction_passed else LOCKED_NOTICE,
        )
    return replace(selection, notice=LOCKED_NOTICE)


def _reopened_selection(definitions: tuple[ToolDescriptor, ...]) -> ModelToolSelection:
    """无可交付改动：恢复首次修改前的调查与修改工具，测试与Review门禁随新Patch照旧生效。"""
    hidden = _ALL_GIT_REVIEW_TOOLS | _HIDDEN_WORKFLOW_ACTIONS | {"read_artifact"}
    return ModelToolSelection(
        tuple(
            tool
            for tool in definitions
            if tool.name not in hidden and not tool.name.startswith("run_profile.")
        )
    )


@dataclass(frozen=True, slots=True)
class CodingWorkflowToolView:
    """Expose only the tools valid for the current single-profile coding phase."""

    require_pre_edit_profile: bool = True
    enable_planning: bool = False
    allow_optional_pre_edit_profile: bool = False
    reserve_closing_phase: bool = False
    # 账本推算的可负担请求数降到该值时，首次修改前的下一请求强制提交Patch。
    patch_deadline_requests: int | None = None
    # Review显示工作区净改动为空（如后续Patch撤销了修复）时重新开放调查与修改，而不是关闭目录。
    reopen_after_clean_review: bool = False
    # 复现草稿目录中的改动不算可交付改动（草稿永不进入补丁）。
    scratch_reproduction: bool = False
    # 向模型说明本步工具受限的原因与解锁条件；关闭时选择结果逐字不变。
    explain_tool_gates: bool = False
    # 公开测试Profile超时（被测代码或测试挂起）按代码级失败进入诊断修复，而非关闭目录。
    test_timeouts_are_failures: bool = False
    # 最近一次Patch失败后的强制重试保留read_file：参数错误常需重读以取得正确SHA或锚点。
    keep_reads_after_failed_patch: bool = False
    # 首次以可交付改动收尾时重新开放一轮自我校验（覆盖问题隐含的边界并回归既有测试）。
    self_verification: bool = False
    # 连续多次修改后测试仍失败时开放Review收尾（剩余失败可能早已存在或断言旧行为）。
    bounded_repair_cycles: bool = False
    # 净改动为空而重新开放修改时说明原因（后续Patch撤销了修复，提交将为空）。
    explain_clean_review: bool = False
    # 参数校验拒绝的Profile调用（从未运行）不视为测试失败或基础设施故障。
    profile_argument_errors_recoverable: bool = False
    # 修复循环硬上限：连续8次验证失败后只开放Review收尾（需bounded_repair_cycles）。
    force_review_after_repairs: bool = False
    # 自我校验另要求检查同一行为的其他实现（同类/子类、其他后端或打印器、重复辅助函数）。
    sibling_scope_check: bool = False
    # 首次改源码前须有一次复现草稿测试以代码级失败结束（有界自动解锁）；自我校验原样运行问题示例。
    require_failing_reproduction: bool = False

    def __post_init__(self) -> None:
        if self.patch_deadline_requests is not None and (
            type(self.patch_deadline_requests) is not int
            or not 1 <= self.patch_deadline_requests <= 16
        ):
            raise ValueError("Patch截止请求数必须是1到16")
        if type(self.scratch_reproduction) is not bool:
            raise ValueError("复现草稿开关必须是布尔值")
        if type(self.explain_tool_gates) is not bool:
            raise ValueError("工具门禁说明开关必须是布尔值")
        if type(self.test_timeouts_are_failures) is not bool:
            raise ValueError("测试超时判定开关必须是布尔值")
        if type(self.keep_reads_after_failed_patch) is not bool:
            raise ValueError("失败Patch后保留读取开关必须是布尔值")
        if type(self.self_verification) is not bool:
            raise ValueError("自我校验开关必须是布尔值")
        if self.self_verification and not self.explain_tool_gates:
            raise ValueError("自我校验依赖工具门禁说明传达校验要求")
        if type(self.bounded_repair_cycles) is not bool:
            raise ValueError("修复循环上限开关必须是布尔值")
        if self.bounded_repair_cycles and not self.explain_tool_gates:
            raise ValueError("修复循环上限依赖工具门禁说明传达收尾条件")
        if type(self.profile_argument_errors_recoverable) is not bool:
            raise ValueError("Profile参数错误可恢复开关必须是布尔值")
        if type(self.require_failing_reproduction) is not bool:
            raise ValueError("复现门禁开关必须是布尔值")
        if self.require_failing_reproduction and not (
            self.scratch_reproduction and self.explain_tool_gates
        ):
            raise ValueError("复现门禁依赖复现草稿与工具门禁说明")
        if type(self.sibling_scope_check) is not bool:
            raise ValueError("同类实现检查开关必须是布尔值")
        if self.sibling_scope_check and not self.self_verification:
            raise ValueError("同类实现检查依赖自我校验")
        if type(self.force_review_after_repairs) is not bool:
            raise ValueError("修复循环硬上限开关必须是布尔值")
        if self.force_review_after_repairs and not self.bounded_repair_cycles:
            raise ValueError("修复循环硬上限依赖修复循环上限")
        if type(self.explain_clean_review) is not bool:
            raise ValueError("空改动说明开关必须是布尔值")
        if self.explain_clean_review and not (
            self.reopen_after_clean_review and self.explain_tool_gates
        ):
            raise ValueError("空改动说明依赖净改动为空时重新开放及工具门禁说明")
        if type(self.reopen_after_clean_review) is not bool:
            raise ValueError("净改动为空时重新开放的开关必须是布尔值")
        if type(self.reserve_closing_phase) is not bool:
            raise ValueError("调查收尾工具目录开关必须是布尔值")
        if type(self.enable_planning) is not bool:
            raise ValueError("规划工具目录开关必须是布尔值")
        if type(self.allow_optional_pre_edit_profile) is not bool:
            raise ValueError("可选预测试工具目录开关必须是布尔值")
        if self.allow_optional_pre_edit_profile and self.require_pre_edit_profile is not False:
            raise ValueError("可选预测试不能同时启用强制修改前基线")

    def admit_call(self, turn: Turn, call: ToolCallContent) -> AgentFailure | None:
        """复现门禁未解锁时拒绝触及源码的Patch；其他调用与关闭门禁时一律放行。"""
        if not self.require_failing_reproduction:
            return None
        return reproduction_admission_failure(turn, call)

    def select(
        self,
        turn: Turn,
        definitions: tuple[ToolDescriptor, ...],
    ) -> ModelToolSelection:
        gate = reproduction_gate_state(turn) if self.require_failing_reproduction else None
        if gate is not None:
            turn = without_reproduction_rejections(turn)
        workflow_definitions = (
            tuple(tool for tool in definitions if tool.name != UPDATE_PLAN_TOOL)
            if self.enable_planning
            else definitions
        )
        if self.scratch_reproduction:
            turn = _without_scratch_edits(turn)
        if self.test_timeouts_are_failures:
            turn = with_test_timeouts_as_failures(turn)
        if self.profile_argument_errors_recoverable:
            turn = without_rejected_profile_arguments(turn)
        selection = self._select_workflow(turn, workflow_definitions)
        if self.bounded_repair_cycles:
            selection = _with_bounded_repair_cycles(
                turn,
                selection,
                workflow_definitions,
                force_review=self.force_review_after_repairs,
            )
        if (
            self.self_verification
            and not selection.tools
            and _first_closure_with_changes(
                _observations(turn), ignore_scratch=self.scratch_reproduction
            )
        ):
            selection = _verification_selection(
                workflow_definitions,
                sibling_scope=self.sibling_scope_check,
                verbatim_example=self.require_failing_reproduction,
            )
        if (
            self.reopen_after_clean_review
            and not selection.tools
            and _latest_review_is_clean(
                _observations(turn), ignore_scratch=self.scratch_reproduction
            )
        ):
            selection = _reopened_selection(workflow_definitions)
            patches = [tool.name for tool in selection.tools if tool.name in _PATCH_TOOLS]
            if self.explain_clean_review and patches:
                selection = replace(selection, notice=_CLEAN_REVIEW_NOTICE.format(patch=patches[0]))
        if self.keep_reads_after_failed_patch:
            selection = _with_reads_after_failed_patch(turn, selection, workflow_definitions)
        if self.patch_deadline_requests is not None:
            selection = _enforce_patch_deadline(
                turn, selection, workflow_definitions, self.patch_deadline_requests
            )
        if self.reserve_closing_phase:
            selection = self._prefer_closing(turn, selection)
        if gate is not None:
            selection = _with_reproduction_lock(gate, selection, workflow_definitions)
        baseline_pending = (
            self.require_pre_edit_profile
            and any(tool.name.startswith("run_profile.") for tool in selection.tools)
            and not any(
                observation.tool.startswith("run_profile.") for observation in _observations(turn)
            )
        )
        # 只开放调查/修复窗口，不绕过强制测试、Review或失败后的空目录。
        if (
            self.enable_planning
            and not baseline_pending
            and selection.forced_tool is None
            and any(tool.name in _SOURCE_INSPECTION_TOOLS for tool in selection.tools)
        ):
            names = {tool.name for tool in selection.tools} | {UPDATE_PLAN_TOOL}
            selection = replace(
                selection, tools=tuple(tool for tool in definitions if tool.name in names)
            )
        if self.allow_optional_pre_edit_profile:
            selection = _with_exclusive_profile_patch(selection)
        if not self.explain_tool_gates:
            return replace(selection, notice=None)
        if not selection.tools:
            return replace(selection, notice=_NO_TOOLS_NOTICE)
        return selection

    def _prefer_closing(self, turn: Turn, selection: ModelToolSelection) -> ModelToolSelection:
        """只结束首次修改前的自由调查；失败重读、测试和Review门禁不受影响。"""
        names = {tool.name for tool in selection.tools}
        patches = names & _PATCH_TOOLS
        observations = _observations(turn)
        latest_user_index = max(
            (
                index
                for index, item in enumerate(turn.items)
                if isinstance(item.content, TextContent) and item.content.kind == "user_message"
            ),
            default=-1,
        )
        current_source = tuple(item for item in observations if item.index > latest_user_index)
        if (
            selection.forced_tool is not None
            or selection.choice != "auto"
            or len(patches) != 1
            or not names & _SOURCE_INSPECTION_TOOLS
            or any(item.tool in _PATCH_TOOLS for item in observations)
            or any(name.startswith("run_profile.") for name in names)
            or not _has_edit_snapshot(current_source)
            or not prefer_closing_over_investigation(turn)
        ):
            return selection
        # 完整SHA不代表编辑上下文充分；保留定点读取补齐相关实现，避免强迫猜测。
        # 只收敛广泛搜索，不强迫写入；预算和Patch新鲜度仍由原执行层核验。
        focused_tools = patches | {"read_file"}
        return ModelToolSelection(
            tuple(tool for tool in selection.tools if tool.name in focused_tools)
        )

    def _select_workflow(
        self,
        turn: Turn,
        definitions: tuple[ToolDescriptor, ...],
    ) -> ModelToolSelection:
        observations = _observations(turn)
        if _workflow_failed(observations):
            return ModelToolSelection(())

        by_name = {definition.name: definition for definition in definitions}
        profiles = tuple(name for name in by_name if name.startswith("run_profile."))
        patches = tuple(name for name in by_name if name in _PATCH_TOOLS)
        if not _workflow_is_supported(definitions, profiles, patches):
            return ModelToolSelection(() if self.allow_optional_pre_edit_profile else definitions)

        profile = profiles[0] if profiles else None
        if self.allow_optional_pre_edit_profile and profile is None:
            return ModelToolSelection(())
        patch = patches[0]
        patch_results = tuple(item for item in observations if item.tool == patch)
        successful_patches = tuple(
            item.index for item in patch_results if item.result.outcome == "succeeded"
        )
        last_patch = successful_patches[-1] if successful_patches else -1
        if profile is None:
            return _without_profile_selection(
                definitions, observations, patch, patch_results, last_patch
            )
        return _with_profile_selection(
            definitions,
            observations,
            profile,
            patch,
            patch_results,
            last_patch,
            require_pre_edit_profile=self.require_pre_edit_profile,
            allow_optional_pre_edit_profile=self.allow_optional_pre_edit_profile,
        )


def _without_profile_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    patch: str,
    patch_results: tuple[_Observation, ...],
    last_patch: int,
) -> ModelToolSelection:
    """无Profile流程只使用原读取、Patch与Review事实。"""
    if last_patch < 0:
        return _pre_patch_selection(
            definitions,
            observations,
            patch,
            patch_results,
        )
    review = _git_review_selection(definitions, observations, last_patch)
    if review.tools:
        return review
    if any(
        item.index > last_patch
        and item.tool in _ALL_GIT_REVIEW_TOOLS
        and item.result.outcome != "succeeded"
        for item in observations
    ):
        return ModelToolSelection(())
    review_index = max(
        item.index
        for item in observations
        if item.index > last_patch
        and item.tool in _ALL_GIT_REVIEW_TOOLS
        and item.result.outcome == "succeeded"
    )
    names = {"read_file"}
    patched_paths = _patched_paths_requiring_reread(observations, last_patch)
    if patched_paths <= _successful_reads_after(observations, review_index):
        names.add(patch)
    return ModelToolSelection(tuple(item for item in definitions if item.name in names))


def _with_profile_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    profile: str,
    patch: str,
    patch_results: tuple[_Observation, ...],
    last_patch: int,
    *,
    require_pre_edit_profile: bool,
    allow_optional_pre_edit_profile: bool = False,
) -> ModelToolSelection:
    """有Profile流程强制测试；只有确定代码失败才能进入修复。"""
    unsuccessful_patch_results = tuple(
        item
        for item in patch_results
        if item.index > last_patch and item.result.outcome != "succeeded"
    )
    if unsuccessful_patch_results:
        return _failed_patch_selection(
            definitions,
            observations,
            unsuccessful_patch_results[-1],
        )
    if not require_pre_edit_profile and last_patch < 0:
        if allow_optional_pre_edit_profile:
            return _optional_pre_edit_selection(definitions, observations, profile, patch)
        return _pre_patch_selection(
            definitions,
            observations,
            patch,
            patch_results,
            bound_no_profile_reads=False,
        )

    profile_results = tuple(
        item for item in observations if item.tool == profile and item.index > last_patch
    )

    if not profile_results:
        if last_patch >= 0:
            return _selection(definitions, {profile}, forced_tool=profile)
        names = (
            {tool.name for tool in definitions}
            - _ALL_GIT_REVIEW_TOOLS
            - _HIDDEN_WORKFLOW_ACTIONS
            - {patch}
            - {"read_artifact"}
        )
        return _selection(definitions, names)

    validation = profile_results[-1]
    if validation.result.outcome == "succeeded" and last_patch >= 0:
        return _git_review_selection(definitions, observations, validation.index)

    if _is_code_test_failure(validation.result) or (
        validation.result.outcome == "succeeded" and last_patch < 0
    ):
        return _validation_diagnosis_selection(
            definitions, observations, validation, profile, patch, last_patch
        )

    return ModelToolSelection(())


def _optional_pre_edit_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    profile: str,
    patch: str,
) -> ModelToolSelection:
    """预测试只提供观察；不强迫测试/修改，也不能抵扣修改后验证。"""
    names = {*_SOURCE_INSPECTION_TOOLS, profile, patch}
    validations = tuple(item for item in observations if item.tool == profile)
    if validations and not _diagnostic_is_complete(validations[-1].result):
        names.add("read_artifact")
    return ModelToolSelection(tuple(item for item in definitions if item.name in names))


def _validation_diagnosis_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    validation: _Observation,
    profile: str,
    patch: str,
    last_patch: int,
) -> ModelToolSelection:
    """非零退出不等于测试通过；读取故障证据后才重新开放Patch。"""
    if last_patch >= 0:
        # 故障可能来自代码或测试目标；保留原只读工具和同一受控Profile。
        names = {*_SOURCE_INSPECTION_TOOLS, profile}
        if not _diagnostic_is_complete(validation.result):
            names.add("read_artifact")
        patched_paths = _patched_paths_requiring_reread(observations, last_patch)
        unread = patched_paths - _successful_reads_after(observations, validation.index)
        if not unread:
            names.add(patch)
            return _selection(definitions, names)
        return replace(_selection(definitions, names), notice=_reread_notice(patch, unread))
    names = {
        item.name
        for item in definitions
        if item.effect_class == EffectClass.READ_ONLY and item.name not in _ALL_GIT_REVIEW_TOOLS
    }
    if _diagnostic_is_complete(validation.result):
        names.discard("read_artifact")
    names.add(patch)
    return _selection(definitions, names)
