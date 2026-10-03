---
doc_type: validation-evidence
status: current
version: 2
code_revision: 0b1e16ab8482ec324e35f81532ec58a1d09b1b6b
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_first_failure_projection.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# Windows首失败v2单次原生结果验证

## 1. 结论与范围

**v2首失败九字段已经在单次Windows运行取得；原SDK、分支与proof门未通过，Git128根因仍UNKNOWN。**

唯一[Run37118277852](https://github.com/carrie1988/Harnessix/actions/runs/37118277852)实际
attempt1、headSHA=`0b1e16ab8482ec324e35f81532ec58a1d09b1b6b`、completed/failure。
有限result为EXECUTION_INCOMPLETE，execution_performed=true，debugger_exit及pytest_exit均1，
timed_out及log_limit_stopped均false。Job111189210671使用windows-latest；上传有限artifact成功。
旧Run37103867851不重跑、不覆盖；只发出一次原workflow_dispatch，不重试或延长原期限。

背景、版本隔离、九字段校验、固定十八输入及原完整门见
[详细设计第13～14节](../../changes/m09-r4-windows-trace2-role-input-binding.md#13-首失败九字段有限发布与固定输入接合)。
本验证不修改源码、工具、Schema、输入或支持平台；结果身份以当前固定候选为准，不继承新主仓或其他专项成绩。

## 2. 首失败实际值与源码解释

外层v3，failure sibling为`harnessix.git-native-failure-observation/v2`/FINITE_AB；
A/B post_worker_failure_status均valid，field_states.post_worker_failure均FINITE，九字段值全同。

| 字段 | A/B实际值 |
|---|---|
| schema | harnessix.git-material-worker-failure/v1 |
| origin | pre_cleanup |
| stage | git_validate |
| error_code | git_material_git_failed |
| handler_error_code | git_material_git_failed |
| git_popen_returned | true |
| git_returncode | 128 |
| git_stdout_complete | null |
| git_stdout_expected | null |

九键集合、类型、有限枚举和原案例返回码关联已验证；当前原validator亦接受这两条记录。
两个null为原JSON实测值，不是缺失占位或REJECTED升级。
固定[`git_material_worker.py`](https://github.com/carrie1988/Harnessix/blob/0b1e16ab8482ec324e35f81532ec58a1d09b1b6b/src/harnessix/delivery/git_material_worker.py#L190-L208)
先判断reader存活/错误及非零退出，再判断stdout完整和预期OID；128加两个null与该短路一致。
这是源码条件性解释，不证明stdout为空、stdin正确、对象写入完成或Git内部失败原因。

## 3. 原门与信任边界

| 原门或有限观察 | 实际结果 |
|---|---|
| A/B call、Worker、Git、original_operation_returned | failed、2、128、false |
| original_sdk_acceptance / branch_gate_passed | false / false |
| proof | ABSENT，两例一致 |
| 两独立PID完整材料分支见证 | false，invocations为空 |
| ARM / branch / bad-marker计数 | 0 / 0 / 0 |
| Trace2 | MATCHED、MATCHED_128；completeness UNKNOWN、UNCLASSIFIED_FORMAT |
| Trace2阶段及错误格式 | ENTRY/DISPATCH/REPO；HASH_OBJECT_ADD_AGGREGATE |
| 历史结果及根因 | FAIL_RETAINED / UNKNOWN |

所有观察为UNAUTHENTICATED_DIAGNOSTIC_ONLY，案例仅顺序A/B映射，不是Owner PID认证。
原raw_validation=OBSERVED_V5_VERIFIED_NOT_INDEPENDENT_MAC，未做独立Worker MAC验真。
0、false或null不能证明没有创建Git、没有外部效果或原分支回调一定未发生。
有限字段合法不改变任何业务批准或原门，不用于R4或1.0发布成功声明。

## 4. 原始有限Artifact与固定输入

artifact11272222412恰好两个根JSON；下载ZIP1895字节，GitHub声明及计算SHA256同为
`2516721afa453c036112c5580e37cbd5442aba98ec345baa5ccdb3dd7d5402df`。
ZIP只在内存核验一次，没有额外成员、重复名、加密或符号链接；JSON拒绝重复键和非有限常数。

| 原有限文件 | 字节 | SHA256 |
|---|---:|---|
| [result.json](result.json) | 6553 | b1a7ca84765e540148783cb167ff02c778f2649c4ecbf946958cbbcc25bf4301 |
| [result-sha256.json](result-sha256.json) | 87 | 6804b3001e30fad6ce98f3b647fed59aa1c10ad1898c3c71f436829a8e8f9b3c |

两文件按原字节保存，未重序列化；原声明与result摘要匹配。
初次归档提交`328bb08`中Git文本规范化曾将Windows摘要回执从87字节变为86字节，
该提交的摘要字节不符合原件身份，失败对照保留。当前通过
[两条定向行尾规则](../../../.gitattributes)固定原始文件，不影响其他资料；
index与最终提交Git blob均须独立核对原件SHA，而不是只检查工作目录字节。
新增规则后仅对两件原件执行定向`git add --renormalize`，确保已有index条目按新属性重算，
不对整个验证目录或其他历史原件进行规范化。
result.authorized_revision与Run headSHA精确匹配；metadata_sha256为原合同摘要
`b07e5b8b9d5ba66b83892a6816b48b02d9ec9ce6ab22a00c3bd46ef20dfcf544`。
十八项固定名单、LF及唯一LF→CRLF字节都重新核验；pairs_matched=true，现场工具存在，原预检未报告失败阶段。
这些证据验证有限结果和输入身份，不证明未经公开的运行细节或全仓源码均已验收。

期限保持command20s、operation45s、watchdog240s、step300s、Job15min；A/B各一次，原13hook不变，
有限artifact保留14天。没有获取raw、stderr、CDB或Job业务日志，没有读取模型凭据或调用模型。

## 5. 交付与后继

[verification.json](verification.json)记录Run/Job/artifact、摘要、十八输入身份、九字段、原门和未证明项。
私有单次报告、API metadata、原有限JSON及manifest位于
`~/Library/Application Support/Harnessix/verification/windows-first-failure-native-20261003-v1/execution-addendum-v1/`，
目录0700、文件0600，旧原件保持只读。

后继以原git_validate前的数据供给、Git对象库写入与调试布防为接缝做有限源码求证和独立反例。
不以重复同一Run替代根因分析，不从诊断合法推导完整Git、Backupv2、Windows消费者、R3、Beta或商用门禁完成。
