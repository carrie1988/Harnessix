"""Derive a compact coding-tool catalog from durable turn facts."""

from __future__ import annotations

from dataclasses import dataclass, replace

from harnessix.agent.models import (
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    Turn,
)
from harnessix.agent.planning import UPDATE_PLAN_TOOL
from harnessix.agent.ports import ModelToolSelection
from harnessix.delivery.rollback_action import WORKSPACE_ROLLBACK_TOOL
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass, ToolDescriptor

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
    return _selection(definitions, {"read_file"}, forced_tool="read_file")


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


@dataclass(frozen=True, slots=True)
class CodingWorkflowToolView:
    """Expose only the tools valid for the current single-profile coding phase."""

    require_pre_edit_profile: bool = True
    enable_planning: bool = False
    allow_optional_pre_edit_profile: bool = False

    def __post_init__(self) -> None:
        if type(self.enable_planning) is not bool:
            raise ValueError("规划工具目录开关必须是布尔值")
        if type(self.allow_optional_pre_edit_profile) is not bool:
            raise ValueError("可选预测试工具目录开关必须是布尔值")
        if self.allow_optional_pre_edit_profile and self.require_pre_edit_profile is not False:
            raise ValueError("可选预测试不能同时启用强制修改前基线")

    def select(
        self,
        turn: Turn,
        definitions: tuple[ToolDescriptor, ...],
    ) -> ModelToolSelection:
        workflow_definitions = (
            tuple(tool for tool in definitions if tool.name != UPDATE_PLAN_TOOL)
            if self.enable_planning
            else definitions
        )
        selection = self._select_workflow(turn, workflow_definitions)
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
        return (
            _with_exclusive_profile_patch(selection)
            if self.allow_optional_pre_edit_profile
            else selection
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
        if patched_paths <= _successful_reads_after(observations, validation.index):
            names.add(patch)
        return _selection(definitions, names)
    names = {
        item.name
        for item in definitions
        if item.effect_class == EffectClass.READ_ONLY and item.name not in _ALL_GIT_REVIEW_TOOLS
    }
    if _diagnostic_is_complete(validation.result):
        names.discard("read_artifact")
    names.add(patch)
    return _selection(definitions, names)
