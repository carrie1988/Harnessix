---
doc_type: validation-evidence
status: current
version: 1
code_revision: 1df5aceb995fe96419ca2ea04b046a3be022f965
owners: [core]
modules: [product-config, session, trusted-actions]
related_adrs:
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_product_state_owner.py
  - tests/product_config/test_product_state_owner_windows.py
supersedes: []
---

# 全状态Owner评审资料

## 1. 评审结论范围

评审对象为固定`1df5ace`的根外静默Owner、默认产品最外层接线、Action非重入借用及目录线程取消结算。
不是完整备份恢复、三平台发行或1.0商用验收。R1～R6保持开放。

## 2. 必读源码与设计

1. [总体与详细设计](../../changes/m09-r1-product-state-ownership.md)：核对原缺口、OWN-1～6、完整状态盘点及限制。
2. [产品入口](../../../src/harnessix/product_config/server.py)：确认锁早于Root、Store与Provider，关闭晚于全部资源。
3. [原Owner](../../../src/harnessix/product_config/state_owner.py)：核对地址折叠、原私有锁、不删除、借用失效与固定错误。
4. [POSIX端口](../../../src/harnessix/product_config/state_owner_posix.py)：核对FD路径身份、权限和Darwin ACL，不把目录mtime当身份。
5. [Windows端口](../../../src/harnessix/product_config/state_owner_windows.py)：父链先验后创建、Junction拒绝和不共享私有Handle。
6. [Action借用](../../../src/harnessix/product_config/action_owner.py)：没有第二根内锁，不把业务错误重分类，不关闭外层Owner。
7. [原工作结算](../../../src/harnessix/session/maintenance_io.py)：父取消只设置原控制，不抛弃线程或发第二次工作。

## 3. 必须核对的证据

- [原入口RED](logs/source-order-red.txt)真实到达配置Store；新最终测试在此之前拒绝。
- [3.13受影响回归](logs/affected-python313-final.txt)与[独立3.12回归](logs/independent-affected-python312.txt)
  各1578通过/33跳过，不能相加；[专项](logs/focused.txt)及[独立专项](logs/independent-focused-python312.txt)各53通过/7跳过。
- Root改名互斥、真实进程终止、宽权限/ACL/链接、Owner借用及重复取消为原生本机证据；七项Windows专项为skip。
- [固定合同事实](contract-facts.json)、[结构化执行](verification.json)、[Wheel字节观察](wheel-observation.json)
  与[Manifest](bundle-manifest.json)须逐项一致，不能由一份JSON自身声明推导全部通过。

## 4. 风险与不得推导的结论

- 锁不是原Key身份、Event/Artifact来源或业务静默的完整证明。
- 同UID恶意删除锚点、非合作SQLite写入者、旧程序并发及特殊文件系统不在保证内。
- 线程期限是合作期限；内核调用不可强杀，不能报告无条件5秒硬结束。
- Root改名测试不证明多库复制一致、候选来源认证或整体恢复发布崩溃结算。
- Wheel0.1.0测试不证明正式安装、Windows写入、许可处置、Beta或商用发布。

## 5. 尚未关闭的真实工作

完整产品停机备份/恢复必须包括六库、可选Process事实、Blob及独立Key，原来源验证先于替换，
坏候选或错Key保留当前状态，崩溃与取消按整体发布事实结算。原UNKNOWN不重放。
Windows原生核心编码、固定Task Pack真实质量、12件依赖许可、实际发行输入及Beta仍进入原R1～R6门禁。
