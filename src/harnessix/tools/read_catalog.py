"""组装Workspace只读工具的版本化合同与目录，不开启文件或执行工具。"""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactPage, ReadArtifactInput
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.tools import git
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
    ReadFileInputV4,
    ReadFileOutput,
    ReadFileSnapshotOutput,
    ReadFileViewOutput,
)
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitDiffOutput,
    GitReviewInput,
    GitReviewOutput,
    GitStatusInput,
    GitStatusOutput,
)
from harnessix.tools.patterns import GREP_INCLUDE_RULE
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
    RegexGrepInput,
    SearchInput,
)
from harnessix.tools.search_execution_contract import (
    execution_contract as search_execution_contract,
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

# ADR 0162：读取时限到了而已有命中时返回这些命中（Beta Issue #24）。
_TIME_LIMIT_NOTE = (
    "读取时限内没搜完时返回已找到的命中，truncation_reason为time_limit："
    "其余文件没有搜过，需要完整结果时缩小path或收窄include再搜"
)

_SNAPSHOT_GREP_BINDING = _ReadBinding(
    "grep",
    "按大小写敏感字面量搜索，不支持正则；默认前后各2行（最多20行）、最多10项。"
    "include不含“/”时按文件名匹配任意深度（如*.py），含“/”时按起点相对路径匹配。"
    "每项content_sha256来自同次完整原始文件读取，可作Patch expected_sha256；"
    "revision不是内容SHA。片段截断与文件摘要完整性独立，证据不足仍用read_file扩读。"
    + _TIME_LIMIT_NOTE,
    GrepSnapshotInput,
    GrepSnapshotOutput,
)


_REGEX_GREP_BINDING = _ReadBinding(
    "grep",
    "逐行搜索，match_mode默认regex：query是原样RE2表达式，不隐式修正；"
    "可显式用literal查找大小写敏感字面量。支持交替、可选空白、转义元字符和Unicode，"
    "不支持反向引用或前后查找断言；无效模式会明确失败。"
    "正则无命中且查询含元字符时自动按字面量重搜，结果标记literal_fallback。"
    "默认前后各2行（最多20行）、最多10项；每行仅返回首个命中片段，不跨行、不归一化文本。"
    "include不含“/”时按文件名匹配任意深度（如*.py），含“/”时按起点相对路径匹配。"
    "每项content_sha256来自同次完整原始读取，可作Patch expected_sha256；"
    "revision不是内容SHA，证据不足仍用read_file扩读。" + _TIME_LIMIT_NOTE,
    RegexGrepInput,
    GrepSnapshotOutput,
)

_DEFINITION_READ_BINDING = _ReadBinding(
    "read_file",
    "读取原始文本页及完整SHA。已知Python(.py/.pyi)定义可显式view=definition、唯一def/class头anchor，"
    "同时取得真实声明头和连续实现页；省略前导文档的行范围会明确标记，需文档或其他语言用view=text。"
    "声明头与正文不可拼成原文件，也不可把片段当全文件。分页及expected_revision边界不变",
    ReadFileInputV4,
    ReadFileViewOutput,
)


def _read_bindings(
    enable_grep_snapshots: bool = False,
    enable_regex_search: bool = False,
    enable_definition_views: bool = False,
) -> tuple[_ReadBinding, ...]:
    """显式候选只替换所选绑定，其他只读工具和默认合同保持不变。"""
    if type(enable_grep_snapshots) is not bool:
        raise KernelError("tool_snapshot_policy_invalid", "搜索快照开关必须显式为布尔值")
    if type(enable_regex_search) is not bool:
        raise KernelError("tool_regex_policy_invalid", "正则搜索开关必须显式为布尔值")
    if type(enable_definition_views) is not bool:
        raise KernelError("tool_definition_policy_invalid", "定义视图开关必须显式为布尔值")
    if enable_grep_snapshots and enable_regex_search:
        raise KernelError("tool_search_policy_conflict", "搜索快照与正则搜索开关不能同时启用")
    grep_binding = _REGEX_GREP_BINDING if enable_regex_search else _SNAPSHOT_GREP_BINDING
    return tuple(
        _DEFINITION_READ_BINDING
        if enable_definition_views and b.name == "read_file"
        else grep_binding
        if (enable_grep_snapshots or enable_regex_search) and b.name == "grep"
        else b
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
    if binding.input_model is ReadFileInputV4:
        major = 3
    elif binding.input_model is RegexGrepInput:
        major = 4
    elif binding.output_model is GrepSnapshotOutput:
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
    read_timeout_seconds: int = READ_TIMEOUT_SECONDS,
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
        "timeout": read_timeout_seconds,
    }
    if issubclass(binding.output_model, ReadFileSnapshotOutput):
        rules["random_access"] = "start_line_without_revision_is_a_fresh_observation"
        rules["snapshot"] = {
            "spec_version": "harnessix.read-file-snapshot/v1",
            "algorithm": "sha256",
            "digest_input": "complete_raw_file",
            "max_digest_bytes": MAX_SCAN_BYTES,
            "oversize": "omitted_limit_or_platform_limit_exceeded",
        }
        if binding.output_model is ReadFileViewOutput:
            rules["definition_view"] = {
                "spec_version": "harnessix.definition-view/v1",
                "parser": "python-tokenize/v1",
                "max_source_bytes": MAX_SCAN_BYTES,
                "max_header_bytes": MAX_LINE_BYTES,
                "max_source_lines": 100_000,
                "source_execution": False,
                "omission": "explicit_leading_literal_docstring_range",
            }
    if issubclass(binding.input_model, SearchInput):
        rules["search"] = search_execution_contract(
            enable_regex_search=binding.input_model is RegexGrepInput,
            snapshot_grep=binding.input_model is GrepSnapshotInput,
        )
        if issubclass(binding.input_model, GrepInput):
            rules["grep_include"] = GREP_INCLUDE_RULE
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
    read_timeout_seconds: int = READ_TIMEOUT_SECONDS,
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
                    read_timeout_seconds=read_timeout_seconds,
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
