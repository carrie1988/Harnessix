---
doc_type: validation-evidence
status: current
version: 1
code_revision: 23f12bbeb65df8b3fc0b1a94c66d9d8b45910abf
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
  - tests/governance/test_windows_git_native_selected_layout.py
supersedes: []
---

# 固定 Git 布局候选的原生执行不完整验证报告

## 1. 结论与边界

唯一固定提交23f12bbeb65df8b3fc0b1a94c66d9d8b45910abf的
[Run36983172137](https://github.com/carrie1988/Harnessix/actions/runs/36983172137)、attempt1已结束，终态failure。
十六件发行输入和实际选中映像的原两对完整PE/PDB校验通过；CDB与Python存在并已实际启动执行。
当前退出点已经不是前置布局拒绝，但完整分支见证和原两例验收仍未取得。

有限结果为EXECUTION_INCOMPLETE；debugger_exit=1、pytest_exit=1，timed_out=false、
log_limit_stopped=false，分支invocations和有效cases均为空。原SDK验收仍false，原Git128根因仍UNKNOWN。
这不是Windows核心编码、消费者Windows11、完整Git交付、R3质量或商用发布通过。

## 2. 背景与设计目标

[旧固定Run](../git-native-layout-refusal-2026-10-02-v1/README.md)在实际选中Git的布局判定处拒绝，未进入调试执行。
[布局适配详设](../../changes/m09-r4-git-native-selected-layout.md)仅改变候选角色判定，保持实际selected、
固定身份、发行输入、两个SDK选择器、十三hook及全部期限，不改变生产resolver或PATH。
本包保存修复提交上唯一一次原workflow的实际结果，不覆盖历史FAIL，不扩大诊断字段或业务权限。

## 3. 架构、流程与源码位置

总体架构、流程、时序和数据流复用
[原有限诊断设计](../../changes/m09-r4-git-native-branch-preflight-v2.md#4-总体架构)；
当前角色判定使用[既有冻结图](../git-native-selected-layout-2026-10-02-v1/selected-layout.png)。
本结果包没有修改图源或PNG，不声称重新渲染。

实际调用链为原workflow → observe.run → prepare → source_checks → selected_paths → existing_tools
→ download_symbols/check_pair → 执行前recheck → run_debugger → 执行后recheck → observation_result → 两文件发布。
其中源码与两pair身份已通过，调试执行已开始，有限结果未满足完整分支和案例见证。

| 源码接口 | 对应职责和本次证据边界 |
|---|---|
| [prepare/recheck](../../../scripts/windows_git_native_branch_observation/preflight.py) | 固定源码、实际selected、工具、PE/PDB身份及前后重检查；source_input_count=16、pairs_matched=true |
| [run_debugger](../../../scripts/windows_git_native_branch_observation/observe.py) | 原CDB命令、240秒外部watchdog、16MiB日志边界及终态回收；实际退出1，未超时/超限 |
| [run_cases.main/CaseSink](../../../scripts/windows_git_native_branch_observation/run_cases.py) | 原两个SDK selector与有限帧收集；存在pytest_exit=1，无法据公开投影证明两例完整执行 |
| [observation_result](../../../scripts/windows_git_native_branch_observation/observe.py) | 只有两例合法、认证原始流可用、分支完整及原退出要求成立才发布完整见证；不合法/不足的cases统一为空 |
| [branch_records/project_case](../../../scripts/windows_git_native_branch_observation/projection.py) | 严格有限字段、顺序、次数和材料分支；缺失不被升级为通过 |
| [原workflow](../../../.github/workflows/windows-git-native-branch-observation.yml) | 单次手动、attempt1、固定完整revision；本包未修改workflow |

## 4. 运行身份与环境

原windows-latest宿主，Job110762128306，固定head和原workflow。
checkout、锁定依赖安装、结果上传均通过；观察步骤返回失败。
[run.json](run.json)、[jobs.json](jobs.json)、[artifacts.json](artifacts.json)保存API原件。
CDB和解释器SHA见[result.json](result.json)，不导出个人路径、环境或原始Debugger正文。
宿主工具存在且启动不能证明每个业务进程成功，也不外推为Windows11消费者环境验证。

## 5. 数据结构与判定逻辑

结果仍使用harnessix.git-native-branch-observation/v2，固定字段和意义不变：

| 字段 | 实际值与含义 |
|---|---|
| execution_performed | true；原Popen已启动，不等于业务成功 |
| pairs_matched/source_input_count | true/16；原固定准备身份通过，不代表应用授权或SDK成功 |
| debugger_exit/pytest_exit | 1/1；可观察退出码，不据此推断唯一失败原因 |
| branch_gate_passed/original_sdk_acceptance | false/false；无完整分支见证，无原SDK验收 |
| branch_witness.invocations/cases | 空；有限投影没有可发布的完整结果，不能反推两个案例完全未执行 |
| first_failed_stage/reason_code | null/null；未观察到前置拒绝码，不表示所有业务阶段成功 |
| historical_root | UNKNOWN；原Git128因果仍未证明 |

核心判定沿用原实现：前置验真与重检查失败则拒绝；执行后，只有完整两次材料分支、
两个合法原案例以及未超时/未超限同时成立才发布完整见证。否则保留不完整和非零退出。
案例帧可能在收集或严格投影阶段被拒绝；空cases不能确定具体拒绝原因。

## 6. 原件、摘要与复核

Artifact11215628266的[artifact.zip](artifact.zip)为1041字节，API与实际ZIP SHA256均为
33181f03deac6641f7e3bbc75ac3b207f0ec0e950337c0c9cd85467e11d4d247。
仅含result.json和result-sha256.json，逐成员受限读取，不使用任意路径解压。
结果摘要1441a4b82f846171a40b0afac4e6bbf2ee22652b7f689fcc7a8ce53a868f0442与原摘要回执一致。
原摘要回执的CRLF逐字节保留；精确两路径Git属性禁止换行改写，不扩大仓库全局规则。
见[下载核验](download-verification.json)、[汇总](SUMMARY.json)、[复核包](review-packet.json)及[清单](manifest.json)。

## 7. 异常、安全与恢复

原命令20秒、操作45秒、step300秒、watchdog240秒保持；本Run/Job已终态，不存在待继续的本次调试任务。
没有安装/替换Git、解释器或Debugger，没有改变保护或扩大公开原始日志。
原两个FAIL、新不完整结果及费用账本均保留；本次没有新增模型请求或费用结算。
调试器返回1并不单独证明CDB自身初始化失败；pytest返回1也不证明原两个案例的具体失败阶段。

## 8. 后续工作与发布条件

先针对当前帧收集与严格投影进行有限源码求证和离线RED/GREEN复现，保留既有结果。
没有新的实际分支见证时不推断原生Git128根因；不通过更换Git/PATH、删selector、放宽hook或预算来制造通过。
任何新修复须有正式设计、原合同回归与独立证据；后继原生执行必须绑定新的固定候选，不能重跑本Run覆盖失败。
完整Git产品/Backup v2、三平台核心闭环、R3、真实Beta与R1～R6仍须独立完成。
