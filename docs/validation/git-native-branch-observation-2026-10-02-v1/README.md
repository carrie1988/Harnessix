---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: dfba34e707ed845f3e9d844461e124015c22dca7
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
supersedes: []
---

# Windows Git 两处原生返回分支观察验证候选

## 状态与范围

本包是新增手动定点观察入口的离线候选，不是Windows运行报告。基线HEAD为`dfba34e707ed845f3e9d844461e124015c22dca7`；未提交源码由SOURCE及manifest限定身份，不以HEAD替代字节证明。

原Run36949391610 attempt1 FAIL、两例call失败、Git128/worker2/proof缺席完整保留，根因UNKNOWN。没有生产源码修复、没有Windows/CDB执行、没有CI或push，不能关闭Windows/R3/fullCommit或商业GO。

## 文件索引

- [正式设计](../../changes/m09-r4-git-native-branch-observation.md)：边界、接口、固定发行身份、两处分支、失败及部署合同。
- [SOURCE.json](SOURCE.json)：新增候选源码/治理测试与16件既有冻结输入的逐件SHA；不是全仓。
- [COMMANDS.json](COMMANDS.json)：实际本地验证命令及退出结果，不含个人路径。
- [verification.json](verification.json)：真实本地结果、失败保留、原生未验证边界。
- [manifest.json](manifest.json)：本包、SDD、候选源码及16件原输入的最终字节身份；排除自身递归SHA。
- [review-packet.md](review-packet.md)：评审门槛与原生执行前必要授权。

## 验证解释

候选采用已有准备工具的PE/MSF7/PDB规则及两处硬件断点逻辑，不导入私有档案路径或二进制资源。原准备23PASS不作为本候选当前接口覆盖结果。新治理测试使用明确的合成结构及mock进程；helper成功不能替代RVA实际观察。

正式发行PDB在未来现场按固定官方URL、完整ZIP SHA、精确成员SHA和现场PE身份下载核对。仓库仅保存小型公开metadata和源码，没有本地PDB/PE/ZIP、大型SDK或原stderr正文。

实际原生结果将分列`branch_gate_passed`、原pytest退出值与raw/proof有限状态；外部调试条件下`original_sdk_acceptance`始终false。初次原生FAIL保持，未观察到可区分返回前不宣称根因已解决。

## 当前实际离线结果与冻结边界

- 新增合同测试50PASS；关联治理组360PASS，包含上述50项，不与单独批次累加。
- ruff及format覆盖8件Python文件，均通过；26件限域源码输入为10件新增候选加16件原输入，不称全仓。
- 两件旧官方ZIP完整SHA与两对旧发行PE/PDB静态身份重验通过；没有执行这些Git文件，也不证明现场runner身份。
- 三件mmd与SDD图块一致；相邻PNG均Chrome实际渲染并视觉复核，文字可读、无截断。
- 初次47PASS、两次后续50PASS及关联批次原件保留；初次文档门禁5项缺失链接FAIL保留，终态以verification为准。
- 只新增授权限域候选文件；既有workflow、生产源码及其他既有文件不变。候选基线为dfba；后续整合须在发布revision上重新核对冻结输入。
