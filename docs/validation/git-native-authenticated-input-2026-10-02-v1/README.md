---
doc_type: validation-evidence
status: current
version: 1
code_revision: 09a386c5269d476f6d9053c6b1b4c5ad83764372
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

# Windows Git 定点观察发布输入接合验证包

## 文档摘要

本包记录09a386发布输入与既有有限诊断v2的精确接合，不是Windows实际执行或认证业务验收。原v2 730冻结候选的132＋9结果保持历史含义；本轮重新执行正式132项和私有33项，均通过。旧合同对当前pyproject的真实current_source_drift拒绝为1FAIL RED，保留而不放宽规则。

## 精确内容

- [详细设计](../../changes/m09-r4-git-native-authenticated-input-binding.md)：选型、根因、调用、字段、预算、安全及失败语义。
- [SOURCE.json](SOURCE.json)：当前28源码输入、合同双表示及原21/index保持。
- [原contract.json](originals/contract.json)、[原contract.py](originals/contract.py)：接合前原字节；是历史合同快照，不可作为当前运行配置。原合同中的历史身份字段原样保留，不表示新读取或重跑历史Run。
- [facts.json](facts.json)、[verification.json](verification.json)：实际RED、当前132/33与历史132/9分开列示。
- [COMMANDS.json](COMMANDS.json)：精确有限复验命令，不含私有路径或凭据。
- [review-packet.json](review-packet.json)：有限接合范围与后继固定revision技术条件。
- [manifest.json](manifest.json)：新写集及继承不变输入的真实字节SHA；排除自身。

## 范围与限制

只改变contract.json的base_revision和pyproject行bytes/sha256/crlf_bytes/crlf_sha256五个叶字段，contract.py仅CONTRACT_SHA256常量重绑；解析器、source_checks、所有其他合同字段保持。发布pyproject只新增三个精确历史脚本Ruff排除，依赖、安全、测试及打包模型未变；本变更不编辑pyproject。

原21件文件及index字节、原v2 manifest与旧FAIL不回写。旧63成员最初仅pyproject一处漂移；完成接合后旧身份相对当前输入预期为pyproject及两份合同三处差异，因此旧manifest不能当当前运行身份。

四图复用[原v2包](../git-native-branch-preflight-2026-10-02-v2/README.md)已实际渲染/逐图查看的冻结MMD/PNG；本轮只核验图源及像素SHA未变，不声称重新渲染。第二进程独立复算不是另一Agent评审，外部独立审查不能由该标记替代。

官方Git/SDK/PE/PDB、两个selector、13hooks、20/45/300/240秒预算、拒绝非0、UNKNOWN与精确两个artifact均保持。没有Windows/CDB、网络、模型、凭据、Docker、账本、CI或dispatch；输入合法不证明现场工具存在、原生断点通过或商用就绪。

## 最终定点门禁

当前输入正式132、私有33均通过；Ruff/format各10文件、strict mypy仅diagnostics.py 1文件通过。新SDD/README两件实际文档合同零发现，12件精确新写集Secret扫描完整零命中。新manifest排除自身，共73成员，含接合写集及继承的原PNG/固定输入；原21文件、index和旧manifest的SHA保持。新输入合法不推导原生执行或业务成功。
