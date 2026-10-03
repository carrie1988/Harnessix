---
doc_type: validation-evidence
status: current
version: 1
code_revision: ffc653ebc3e6dec9ea67f371562b9581f4bcc9e6
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_material_snapshot_differential.py
  - tests/product_config/test_git_material_native.py
  - tests/governance/test_git_material_snapshot_differential.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# Windows目录写共享兼容与保护验证

## 1. 背景、目标与结论边界

[前置同源原生控制](../windows-existing-fanout-control-2026-10-03-v1/README.md)已取得真实目录共享反例：
原share1下链接被Win32错误32拒绝，仅增加WRITE共享后成功。该包原SDK仍失败，不改写历史结果。
本包落实[总体与详细设计第15节](../../changes/m09-r4-windows-minimum-commit-probe.md#15-windows目录写共享兼容与原保护保留)，
只在原_open用_held_share区分目录3／文件1，两者均禁止DELETE共享。
新修复候选原生尚未取得，暂不登记SDK或Windows商用通过。

## 2. 实现、源码与失败语义

- [Windows句柄端口](../../../src/harnessix/delivery/git_material_native_windows.py#L231)：原access、flags、
  类型、reparse、inode、最终路径、私有DACL、单链接和Resources关闭保留；不提升当前句柄写权限。
- [真实控制测试](../../../tests/product_config/test_git_material_snapshot_differential.py)：原四格和同源两臂，
  历史share1明确构造成测试反例，修复后自然share3仍须真实链接及完整读取成功。
- [原Windows安全负例](../../../tests/product_config/test_git_material_native.py#L74)：文件写入、文件和目录改名／删除拒绝，
  metadata-only控制及关闭后的真实正例保持，原测试未修改。
- [治理及别名反例](../../../tests/governance/test_git_material_snapshot_differential.py)：目录／文件共享位，
  copy2内容和hardlink拒绝、三方lstat集合收敛、实际cross-arm文件／目录符号链接拒绝。
- [原worker后验](../../../src/harnessix/delivery/git_material_worker.py#L251)：原snapshot、command和namespace完整复核后才产生原Proof。

显式真实WINFUNCTYPE以use_last_error=True绑定原kernel32，函数地址与原DLL一致；错误读取紧邻失败调用。
两个独立目录打开前核对同一实际目录身份，不声称在同一个HANDLE上原位改变共享模式。
取消、超时、未知效果、完整OID及Owner／批准逻辑没有更改。

## 3. 验证、安全与交付

九件关联文件实际561通过、7跳过、零失败／错误；包含原RO快照、FD归属、私有目录权限、三方别名及原治理。
原两SDK另行实际2通过、零失败／错误／跳过；不与包含范围重复累加，不外推原生。
Windows相关7项跳过单列；四幅变化Mermaid实际渲染并视觉复核通过，Ruff及精确Secret扫描／自检通过。
首次文档检查发现delivery模块未同步，原发现保留；补现行delivery模块设计后再次检查，不绕过门禁。
新手动候选继续运行原四格、同源两臂、三个真实OS控制、原Windows保护及原两SDK。
原四格一分钟、原SDK五分钟、20／45／240／300正式合同保持，新增保护一步一分钟。
十八输入只更新Windows实现与workflow两行，其余16行、固定PE／PDB、13接点及其他字段保持。

限定独立审查的三个P2在测试域分别补三方非别名收敛、显式last-error使能、两个独立打开措辞；
不把证明加固描述为已经修复生产漏洞，也不从保留原校验调用推定安全验收。
原件、设计前置记录、完整图示、Review Packet和来源差分保存于私有windows-directory-write-share-20261003-v1，
目录0700／文件0600。仅读取Run／Job／step元数据，不读取Git、CDB或Job业务日志。
没有模型调用、Keychain／凭据读取、费用规则变更或原失败重跑。

消费者Windows、完整Git交付／Backup v2、真实R3与费用未决、独立Beta和R1～R6继续开放。
