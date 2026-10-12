"""Derive a compact coding-tool catalog from durable turn facts."""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.models import (
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    Turn,
)
from harnessix.agent.ports import ModelToolSelection
from harnessix.delivery.rollback_action import WORKSPACE_ROLLBACK_TOOL
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass, ToolDescriptor

_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_GIT_REVIEW_TOOLS = frozenset({"git_status", "git_diff"})
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
) -> ModelToolSelection:
    if patch_results:
        return _failed_patch_selection(definitions, observations, patch_results[-1])
    inspections = sum(item.tool in _SOURCE_INSPECTION_TOOLS for item in observations)
    if inspections >= _MAX_NO_PROFILE_INSPECTIONS:
        return _selection(definitions, {patch}, forced_tool=patch)
    return _selection(definitions, {*_SOURCE_INSPECTION_TOOLS, patch})


def _git_review_selection(
    definitions: tuple[ToolDescriptor, ...],
    observations: tuple[_Observation, ...],
    after_index: int,
) -> ModelToolSelection:
    reviewed = {
        item.tool
        for item in observations
        if item.index > after_index
        and item.tool in _GIT_REVIEW_TOOLS
        and item.result.outcome == "succeeded"
    }
    missing_review = set(_GIT_REVIEW_TOOLS) - reviewed
    forced_review = None
    if missing_review:
        forced_review = "git_status" if "git_status" in missing_review else "git_diff"
    return _selection(definitions, missing_review, forced_tool=forced_review)


@dataclass(frozen=True, slots=True)
class CodingWorkflowToolView:
    """Expose only the tools valid for the current single-profile coding phase."""

    require_pre_edit_profile: bool = True

    def select(
        self,
        turn: Turn,
        definitions: tuple[ToolDescriptor, ...],
    ) -> ModelToolSelection:
        observations = _observations(turn)
        if any(_is_infrastructure_failure(item.result) for item in observations) or any(
            item.tool.startswith("run_profile.")
            and item.result.outcome != "succeeded"
            and not _is_code_test_failure(item.result)
            for item in observations
        ):
            return ModelToolSelection(())

        by_name = {definition.name: definition for definition in definitions}
        profiles = tuple(name for name in by_name if name.startswith("run_profile."))
        patches = tuple(name for name in by_name if name in _PATCH_TOOLS)
        if (
            len(profiles) > 1
            or len(patches) != 1
            or not _GIT_REVIEW_TOOLS <= by_name.keys()
            or any(
                definition.effect_class != EffectClass.READ_ONLY
                and definition.name not in {*profiles, *patches, *_HIDDEN_WORKFLOW_ACTIONS}
                for definition in definitions
            )
        ):
            return ModelToolSelection(definitions)

        profile = profiles[0] if profiles else None
        patch = patches[0]
        patch_results = tuple(item for item in observations if item.tool == patch)
        successful_patches = tuple(
            item.index for item in patch_results if item.result.outcome == "succeeded"
        )
        last_patch = successful_patches[-1] if successful_patches else -1
        if profile is None:
            if last_patch < 0:
                return _pre_patch_selection(definitions, observations, patch, patch_results)
            review = _git_review_selection(definitions, observations, last_patch)
            if review.tools:
                return review
            review_index = max(
                item.index
                for item in observations
                if item.index > last_patch
                and item.tool in _GIT_REVIEW_TOOLS
                and item.result.outcome == "succeeded"
            )
            names = {"read_file"}
            patched_paths = _patched_paths_requiring_reread(observations, last_patch)
            if patched_paths <= _successful_reads_after(observations, review_index):
                names.add(patch)
            return ModelToolSelection(tuple(item for item in definitions if item.name in names))

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
        if not self.require_pre_edit_profile and last_patch < 0:
            return _pre_patch_selection(definitions, observations, patch, patch_results)

        profile_results = tuple(
            item for item in observations if item.tool == profile and item.index > last_patch
        )

        if not profile_results:
            if last_patch >= 0:
                return _selection(definitions, {profile}, forced_tool=profile)
            names = (
                set(by_name)
                - _GIT_REVIEW_TOOLS
                - _HIDDEN_WORKFLOW_ACTIONS
                - set(patches)
                - {"read_artifact"}
            )
            return _selection(definitions, names)

        validation = profile_results[-1]
        if validation.result.outcome == "succeeded" and last_patch >= 0:
            return _git_review_selection(definitions, observations, validation.index)

        if _is_code_test_failure(validation.result) or (
            validation.result.outcome == "succeeded" and last_patch < 0
        ):
            if last_patch >= 0:
                names = {"read_file"}
                if not _diagnostic_is_complete(validation.result):
                    names.add("read_artifact")
                patched_paths = _patched_paths_requiring_reread(observations, last_patch)
                if patched_paths <= _successful_reads_after(observations, validation.index):
                    names.add(patch)
                return _selection(definitions, names)
            names = {
                item.name
                for item in definitions
                if item.effect_class == EffectClass.READ_ONLY and item.name not in _GIT_REVIEW_TOOLS
            }
            if _diagnostic_is_complete(validation.result):
                names.discard("read_artifact")
            names.add(patch)
            return _selection(definitions, names)

        return ModelToolSelection(())
