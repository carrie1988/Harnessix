---
doc_type: validation-evidence
status: current
version: 1
code_revision: 82efa8e56e8699dba30b05da6cefbe0ad4beb2f3
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
supersedes: []
---

# Windows 原生观察：真实选中 Git 布局拒绝结果

## 1. 摘要与结论

固定提交 `82efa8e56e8699dba30b05da6cefbe0ad4beb2f3` 的一次真实 Windows 运行在
`selected_git` 前置阶段拒绝，有限原因是 `selected_git_layout_unrecognized`。
这是当前观察器的现场布局准入失败，不是原 Git128 业务故障的根因证明；原失败保持。

## 2. 需求背景与设计依据

认证发行输入接合消除了已知的旧 pyproject 字节不匹配；随后依照原显式执行合同，
仅对新的完整发布提交触发一次运行。设计依据为
[有限拒绝诊断](../../changes/m09-r4-git-native-branch-preflight-v2.md)和
[固定发布输入接合](../../changes/m09-r4-git-native-authenticated-input-binding.md)。
没有修改 SDK 两项选择器、Owner、Job、只读/删除回执保护、十三 hook 或原期限。

## 3. 实际环境与运行身份

- [GitHub Run 36977642845](https://github.com/carrie1988/Harnessix/actions/runs/36977642845)：
  `workflow_dispatch`、attempt 1、固定 head、终态 failure。
- 原 workflow 的 `windows-latest` 原生宿主；不据此声明 Windows11 消费者验收。
- checkout、Python/uv、锁定依赖安装通过；身份预检失败，有限结果上传成功。
- [Run 身份](run.json)、[Job/步骤](jobs.json)、[Artifact API 元数据](artifacts.json)保留原件。

## 4. 当前源码、接口与流程映射

[`prepare`](../../../scripts/windows_git_native_branch_observation/preflight.py) 在平台/输出目录及
固定源输入验真后进入 `selected_paths`。该接口使用实际 `shutil.which("git")` 的结果，
原合法布局仅包括 `cmd/git.exe` 或 `mingw64/bin/git.exe`，其他父目录形状产生固定拒绝。
[`observe`](../../../scripts/windows_git_native_branch_observation/observe.py) 捕获首个有限拒绝，
发布失败阶段、原因和两个 nullable 工具存在标志；不公开原始 PATH、日志、仓库文本或私有凭据。
原调用链及架构/流程/时序/数据流沿用上述详细设计，本资料只记录现场退出点。

## 5. 有限结果与失败语义

[result.json](result.json) 记录 `PREFLIGHT_REFUSED`、`execution_performed=false`、
`branch_gate_passed=false` 和 `original_sdk_acceptance=false`。
两个工具存在标志为 null，说明失败在工具观察之前，不能解读为 CDB/Python 不存在。
尚未执行符号下载、Debugger 或两项 SDK 业务用例，不重跑该 Run，不把拒绝改成通过。

## 6. 数据、摘要与复核

原 [artifact.zip](artifact.zip) 为802字节，API ZIP SHA256 为
`9a088d60ab09508aa41a7217318783aff99468e38751dc3bc02ca7e4713da772`。
只含 result.json 和 result-sha256.json；逐件受限读取，没有抽取任意路径。
两份原始JSON的精确Git属性关闭文本换行转换；原摘要文件的CRLF保持，其他文件规则不变。
[result-sha256.json](result-sha256.json) 与实际结果字节摘要一致；
[下载核验](download-verification.json)、[汇总](SUMMARY.json)、
[Review Packet](review-packet.json)和[成员清单](manifest.json)分别限定身份与结论。

## 7. 安全、取消及资源关闭

结果的合同预算仍为命令20秒、操作45秒、workflow step300秒和外部watchdog240秒。
原生准备已终止，Run/Job为终态；未留待继续的 debugger、SDK测试或模型请求。
原件、旧FAIL和费用账本保持；没有安装SDK、替换Git、改变系统代理或关闭保护机制。

## 8. 后续整改与验收边界

先求证实际选中 launcher 的角色与固定官方 PE/CodeView 身份，再决定最小路径适配。
当前有限字段没有给出 which 绝对路径或具体角色，不以常见安装目录推断现场事实。
任何适配必须保留精确源/PE/PDB验真、SDK选择器及原安全预算，并保留本拒绝原件。
Windows核心编码、完整Git交付/Backup v2、R3、独立Beta及商用R1～R6仍开放。
