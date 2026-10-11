"""Derive a compact coding-tool catalog from durable turn facts."""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.models import (
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    Turn,
)
from harnessix.delivery.rollback_action import WORKSPACE_ROLLBACK_TOOL
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass, ToolDescriptor

_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_GIT_REVIEW_TOOLS = frozenset({"git_status", "git_diff"})
_HIDDEN_WORKFLOW_ACTIONS = frozenset({WORKSPACE_ROLLBACK_TOOL})
_CORRECTION_READ_TOOLS = frozenset({"read_file", "read_artifact"})


@dataclass(frozen=True, slots=True)
class _Observation:
    index: int
    tool: str
    result: ToolResultContent


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
                observations.append(_Observation(index, call.tool, item.content))
    return tuple(observations)


class CodingWorkflowToolView:
    """Expose only the tools valid for the current single-profile coding phase."""

    def select(
        self,
        turn: Turn,
        definitions: tuple[ToolDescriptor, ...],
    ) -> tuple[ToolDescriptor, ...]:
        by_name = {definition.name: definition for definition in definitions}
        profiles = tuple(name for name in by_name if name.startswith("run_profile."))
        patches = tuple(name for name in by_name if name in _PATCH_TOOLS)
        if (
            len(profiles) != 1
            or len(patches) != 1
            or not _GIT_REVIEW_TOOLS <= by_name.keys()
            or any(
                definition.effect_class != EffectClass.READ_ONLY
                and definition.name not in {*profiles, *patches, *_HIDDEN_WORKFLOW_ACTIONS}
                for definition in definitions
            )
        ):
            return definitions

        profile, patch = profiles[0], patches[0]
        observations = _observations(turn)
        successful_patches = tuple(
            item.index
            for item in observations
            if item.tool == patch and item.result.outcome == "succeeded"
        )
        last_patch = successful_patches[-1] if successful_patches else -1
        profile_results = tuple(
            item for item in observations if item.tool == profile and item.index > last_patch
        )

        if not profile_results:
            names = (
                {profile}
                if last_patch >= 0
                else set(by_name) - _GIT_REVIEW_TOOLS - _HIDDEN_WORKFLOW_ACTIONS
            )
            return tuple(item for item in definitions if item.name in names)

        validation = profile_results[-1]
        if validation.result.outcome == "succeeded" and last_patch >= 0:
            reviewed = {
                item.tool
                for item in observations
                if item.index > validation.index
                and item.tool in _GIT_REVIEW_TOOLS
                and item.result.outcome == "succeeded"
            }
            names = set() if reviewed == _GIT_REVIEW_TOOLS else set(_GIT_REVIEW_TOOLS)
            return tuple(item for item in definitions if item.name in names)

        if (
            validation.result.outcome == "failed"
            and validation.result.error is not None
            and validation.result.error.code == "process_nonzero_exit"
        ) or (validation.result.outcome == "succeeded" and last_patch < 0):
            if last_patch >= 0:
                names = set(_CORRECTION_READ_TOOLS)
                names.add(patch)
                return tuple(item for item in definitions if item.name in names)
            names = {
                item.name
                for item in definitions
                if item.effect_class == EffectClass.READ_ONLY and item.name not in _GIT_REVIEW_TOOLS
            }
            names.add(patch)
            return tuple(item for item in definitions if item.name in names)

        return definitions
