"""定点观察的封闭阶段诊断；不发布异常正文或动态参数。"""

from __future__ import annotations

from typing import Literal, TypedDict

Stage = Literal[
    "metadata",
    "execution_revision",
    "platform_paths",
    "source_identity",
    "selected_git",
    "existing_tools",
    "symbols",
    "pe_pdb_identity",
    "scripts",
    "tool_identity",
    "debugger_execution",
    "result_projection",
]

# 值均来自既有函数的字面量错误合同；阶段限制阻止跨阶段误认。
REASONS_BY_STAGE: dict[Stage, frozenset[str]] = {
    "metadata": frozenset({"offline_metadata_sha_mismatch", "duplicate_json_key"}),
    "execution_revision": frozenset({"explicit_fixed_revision_execution_required"}),
    "platform_paths": frozenset(
        {
            "windows_x64_required",
            "private_nonrepository_simple_output_path_required",
            "private_output_preexists",
        }
    ),
    "source_identity": frozenset({"current_source_drift"}),
    "selected_git": frozenset(
        {
            "selected_git_missing",
            "selected_git_layout_unrecognized",
            "selected_git_not_verified_role",
            "selected_git_changed",
        }
    ),
    "existing_tools": frozenset(
        {
            "existing_tools_missing_no_install_performed",
            "existing_cdb_pe_invalid",
            "existing_cdb_x64_required",
        }
    ),
    "symbols": frozenset(
        {
            "official_redirect_refused",
            "official_download_limit",
            "official_download_digest_mismatch",
            "official_symbol_member_mismatch",
        }
    ),
    "pe_pdb_identity": frozenset(
        {
            "pdb_msf_signature_invalid",
            "pdb_msf_bounds_invalid",
            "pdb_stream_block_invalid",
            "pdb_info_missing",
            "pdb_dbi_missing",
            "pdb_symbols_missing",
            "pdb_symbol_bounds_invalid",
            "pdb_branch_symbol_duplicate",
            "pe_dos_signature_invalid",
            "pe_nt_signature_invalid",
            "pe_x64_required",
            "pe_rva_unmapped",
            "official_pair_size_mismatch",
            "official_pair_sha_mismatch",
            "official_pair_image_size_mismatch",
            "official_pair_guid_age_mismatch",
            "official_branch_symbol_rva_mismatch",
            "official_branch_machine_bytes_mismatch",
        }
    ),
    "scripts": frozenset(),
    "tool_identity": frozenset({"existing_tool_changed"}),
    "debugger_execution": frozenset(),
    "result_projection": frozenset({"case_report_invalid", "duplicate_json_key"}),
}


class DiagnosticResult(TypedDict):
    first_failed_stage: Stage | None
    reason_code: str | None
    cdb_file_present: bool | None
    interpreter_file_present: bool | None


class PreflightDiagnostic:
    """只记录首次拒绝位置与已完成的两个is_file观察，不推断根因。"""

    def __init__(self) -> None:
        self._stage: Stage = "metadata"
        self._failure: tuple[Stage, str] | None = None
        self._presence: dict[str, bool | None] = {
            "cdb_file_present": None,
            "interpreter_file_present": None,
        }

    def enter(self, stage: Stage) -> None:
        if type(stage) is not str or stage not in REASONS_BY_STAGE:
            raise ValueError("diagnostic_stage_invalid")
        self._stage = stage

    def presence(
        self, field: Literal["cdb_file_present", "interpreter_file_present"], value: bool
    ) -> None:
        if type(field) is not str or field not in self._presence or type(value) is not bool:
            raise ValueError("diagnostic_presence_invalid")
        self._presence[field] = value

    def capture_failure(self, error: BaseException) -> None:
        if self._failure is not None:
            return
        reason = "UNKNOWN"
        # 不调用str/repr；只比较一个真实str是否等于固定字面量，输出来自白名单。
        if type(error) is ValueError and len(error.args) == 1 and type(error.args[0]) is str:
            for literal in REASONS_BY_STAGE[self._stage]:
                if error.args[0] == literal:
                    reason = literal
                    break
        self._failure = self._stage, reason

    def snapshot(self) -> DiagnosticResult:
        return {
            "first_failed_stage": self._failure[0] if self._failure else None,
            "reason_code": self._failure[1] if self._failure else None,
            "cdb_file_present": self._presence["cdb_file_present"],
            "interpreter_file_present": self._presence["interpreter_file_present"],
        }
