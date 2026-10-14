"""组装Workspace只读工具的版本化合同与目录，不开启文件或执行工具。"""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactPage, ReadArtifactInput
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.tools import git, search
from harnessix.tools.contracts import (
    MAX_DIRECTORY_ENTRIES,
    MAX_LINE_BYTES,
    MAX_RESULT_BYTES,
    MAX_SCAN_BYTES,
    MAX_TEXT_BYTES,
    READ_TIMEOUT_SECONDS,
    ListFilesInput,
    ListFilesOutput,
    ReadContract,
    ReadFileInput,
    ReadFileInputV3,
    ReadFileOutput,
    ReadFileSnapshotOutput,
)
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitDiffOutput,
    GitReviewInput,
    GitReviewOutput,
    GitStatusInput,
    GitStatusOutput,
)
from harnessix.tools.search_contracts import (
    ArchivedGlobOutput,
    ArchivedGrepOutput,
    ArchivedGrepSnapshotOutput,
    GlobInput,
    GlobOutput,
    GrepInput,
    GrepOutput,
    GrepSnapshotInput,
    GrepSnapshotOutput,
    SearchInput,
)
from harnessix.tools.workspace import digest


@dataclass(frozen=True)
class _ReadBinding:
    name: str
    description: str
    input_model: (
        type[ListFilesInput]
        | type[ReadFileInput]
        | type[GlobInput]
        | type[GrepInput]
        | type[GitStatusInput]
        | type[GitDiffInput]
        | type[GitReviewInput]
    )
    output_model: (
        type[ListFilesOutput]
        | type[ReadFileOutput]
        | type[GlobOutput]
        | type[GrepOutput]
        | type[GitStatusOutput]
        | type[GitDiffOutput]
        | type[GitReviewOutput]
    )


_BINDINGS = (
    _ReadBinding(
        "list_files",
        "分页列出目录；不递归、不跟随链接",
        ListFilesInput,
        ListFilesOutput,
    ),
    _ReadBinding(
        "read_file",
        "读取UTF-8文本范围；已知文件可用唯一字面anchor及lines_before定位并取得完整摘要，"
        "无需先grep猜行号；不唯一时明确失败。也可直接指定start_line；"
        "expected_revision绑定先前观察。"
        "digest_status=complete时，content_sha256是完整"
        "文件摘要，可作Patch expected_sha256；max_utf8_bytes限制本页文本，整行分页；不跟随链接",
        ReadFileInputV3,
        ReadFileSnapshotOutput,
    ),
    _ReadBinding(
        "glob",
        "在文件或目录起点定位文件；支持 * ? [] 和 ** 路径段，显式报告截断",
        GlobInput,
        GlobOutput,
    ),
    _ReadBinding(
        "grep",
        "在文件或目录起点按大小写敏感字面量查找行，不支持正则；返回片段和revision。"
        "可用context_lines=1～3同时观察符号附近代码，减少搜索后宽窗口读取；默认0",
        GrepInput,
        GrepOutput,
    ),
)

_SNAPSHOT_GREP_BINDING = _ReadBinding(
    "grep",
    "按大小写敏感字面量搜索，不支持正则；默认前后各2行、最多10项。"
    "每项content_sha256来自同次完整原始文件读取，可作Patch expected_sha256；"
    "revision不是内容SHA。片段截断与文件摘要完整性独立，证据不足仍用read_file扩读",
    GrepSnapshotInput,
    GrepSnapshotOutput,
)


def _read_bindings(enable_grep_snapshots: bool) -> tuple[_ReadBinding, ...]:
    """显式候选只替换搜索绑定，原只读工具和默认合同保持不变。"""
    if type(enable_grep_snapshots) is not bool:
        raise KernelError("tool_snapshot_policy_invalid", "搜索快照开关必须显式为布尔值")
    return tuple(
        _SNAPSHOT_GREP_BINDING if enable_grep_snapshots and b.name == "grep" else b
        for b in _BINDINGS
    )


_GIT_BINDINGS = (
    _ReadBinding(
        "git_status",
        "读取分支和结构化变更状态；不刷新索引或执行Hook",
        GitStatusInput,
        GitStatusOutput,
    ),
    _ReadBinding(
        "git_diff",
        "读取有界工作区或暂存区差异；禁用外部Diff和textconv",
        GitDiffInput,
        GitDiffOutput,
    ),
    _ReadBinding(
        "git_review",
        "顺序聚合结构化Git状态与有界工作区差异；两次只读观察不保证原子一致",
        GitReviewInput,
        GitReviewOutput,
    ),
)


def _descriptor(
    binding: _ReadBinding,
    contract: str,
    *,
    require_approval: bool,
) -> ToolDescriptor:
    major = 1
    if binding.output_model is GrepSnapshotOutput:
        major = 3
    elif binding.output_model in {ReadFileSnapshotOutput, GrepOutput}:
        major = 2
    return ToolDescriptor(
        name=binding.name,
        version=f"{major}.{contract}",
        description=binding.description,
        input_schema=binding.input_model.model_json_schema(),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=require_approval,
        supports_reconciliation=False,
        supports_parallel_calls=True,
    )


def _binding_rules(
    binding: _ReadBinding,
    *,
    implementation: str,
    scope: str,
    max_concurrent_reads: int,
    artifacts: SQLiteArtifactStore | None,
    git_runtime: git.GitReadRuntime | None,
) -> dict[str, object]:
    rules: dict[str, object] = {
        "implementation": implementation,
        "scope": scope,
        "input": binding.input_model.model_json_schema(),
        "output": binding.output_model.model_json_schema(),
        "concurrency": "parallel_read",
        "max_concurrent_reads": max_concurrent_reads,
        "max_text_bytes": MAX_TEXT_BYTES,
        "max_line_bytes": MAX_LINE_BYTES,
        "max_scan_bytes": MAX_SCAN_BYTES,
        "max_directory_entries": MAX_DIRECTORY_ENTRIES,
        "max_result_bytes": MAX_RESULT_BYTES,
        "timeout": READ_TIMEOUT_SECONDS,
    }
    if binding.output_model is ReadFileSnapshotOutput:
        rules["random_access"] = "start_line_without_revision_is_a_fresh_observation"
        rules["snapshot"] = {
            "spec_version": "harnessix.read-file-snapshot/v1",
            "algorithm": "sha256",
            "digest_input": "complete_raw_file",
            "max_digest_bytes": MAX_SCAN_BYTES,
            "oversize": "omitted_limit_or_platform_limit_exceeded",
        }
    if issubclass(binding.input_model, SearchInput):
        rules["search"] = search.execution_contract()
        if binding.output_model is GrepSnapshotOutput:
            rules["snapshot"] = {
                "spec_version": "harnessix.grep-snapshot/v1",
                "algorithm": "sha256",
                "digest_input": "complete_same_observation_raw_file",
            }
        if artifacts is not None:
            rules["artifacts"] = artifacts.contract()
            output: type[ReadContract] = ArchivedGrepOutput
            if binding.name == "glob":
                output = ArchivedGlobOutput
            elif binding.output_model is GrepSnapshotOutput:
                output = ArchivedGrepSnapshotOutput
            rules["output"] = output.model_json_schema()
    if binding in _GIT_BINDINGS:
        assert git_runtime is not None
        rules.update(
            implementation=git_runtime.contract()["implementation"], git=git_runtime.contract()
        )
    return rules


def _artifact_descriptor(
    scope: str,
    artifacts: SQLiteArtifactStore,
    max_concurrent_reads: int,
    *,
    require_approval: bool,
) -> ToolDescriptor:
    contract = digest(
        {
            "scope": scope,
            "artifacts": artifacts.contract(),
            "input": ReadArtifactInput.model_json_schema(),
            "output": ArtifactPage.model_json_schema(),
            "concurrency": "parallel_read",
            "max_concurrent_reads": max_concurrent_reads,
        }
    )
    return ToolDescriptor(
        name="read_artifact",
        version=f"1.{contract}",
        description="分页读取当前会话/工作区JSONL Artifact；过期或损坏失败",
        input_schema=ReadArtifactInput.model_json_schema(),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=require_approval,
        supports_reconciliation=False,
        supports_parallel_calls=True,
    )


def _build_definitions(
    *,
    scope: str,
    implementation: str,
    max_concurrent_reads: int,
    require_approval: bool,
    artifacts: SQLiteArtifactStore | None,
    git_runtime: git.GitReadRuntime | None,
    read_bindings: tuple[_ReadBinding, ...] = _BINDINGS,
) -> dict[str, ToolDescriptor]:
    bindings = (*read_bindings, *(_GIT_BINDINGS if git_runtime is not None else ()))
    definitions = {
        binding.name: _descriptor(
            binding,
            digest(
                _binding_rules(
                    binding,
                    implementation=implementation,
                    scope=scope,
                    max_concurrent_reads=max_concurrent_reads,
                    artifacts=artifacts,
                    git_runtime=git_runtime,
                )
            ),
            require_approval=require_approval,
        )
        for binding in bindings
    }
    if artifacts is not None:
        definitions["read_artifact"] = _artifact_descriptor(
            scope,
            artifacts,
            max_concurrent_reads,
            require_approval=require_approval,
        )
    return definitions
