---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7564a1384eeeabb667b74efdae9a40513713be10
owners: [core]
modules: [product_config, session, artifacts, execution, trusted_actions, delivery, processes]
related_adrs:
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_product_state_restore.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_backup_files_windows.py
supersedes: []
---

# 完整产品恢复与显式结算评审资料

## 1. 对象与边界

评审对象为固定`7564a1384eeeabb667b74efdae9a40513713be10`的POSIX完整产品恢复、根外Journal与启动前未决保护，
不是三平台正式发行、真实模型质量或1.0发布。本切片没有调用线上模型或重置测试费用周期。

## 2. 源码阅读顺序

1. [完整详设](../../changes/m09-r1-product-state-restore.md)：先区分原备份、候选、原Root、Previous与根外来源。
2. [`state_restore_contracts.py`](../../../src/harnessix/product_config/state_restore_contracts.py)：原字节、地址/目录身份、指针Hash、回退与结果字段。
3. [`state_restore.py`](../../../src/harnessix/product_config/state_restore.py)：稳定UUID、期限、Owner、重复请求与有限失败收尾。
4. [`state_restore_prepare.py`](../../../src/harnessix/product_config/state_restore_prepare.py)：完整候选、原Key及合作静默窗口。
5. [`state_restore_journal.py`](../../../src/harnessix/product_config/state_restore_journal.py)：Plan/Pointer/Decision/Result先耐久再命名发布。
6. [`state_restore_flow.py`](../../../src/harnessix/product_config/state_restore_flow.py)：真实S0/S1/S2位置、粘性回退与终态再验真。
7. [`state_owner.py`](../../../src/harnessix/product_config/state_owner.py)、[`server.py`](../../../src/harnessix/product_config/server.py)、[`action_owner.py`](../../../src/harnessix/product_config/action_owner.py)：保护先于初始化。
8. [完整产品测试](../../../tests/product_config/test_product_state_restore.py)：真实六库/认证Artifact/CAS及子进程、取消和确认丢失。

## 3. 必须核查的问题

- 是否逐库覆盖、重新注册Key、补签原历史或构造Executor来恢复？
- Plan是否保存原Manifest字节，原回执是否仍独立授予来源？
- 原Root第一次改名前，Plan和活动指针是否都已耐久发布？
- 正常启动是否在任何Root/Key/Store/Provider创建之前拒绝未决指针？
- Native Rename已提交但返回失败时是否按真实目录身份核对，是否重复执行切换？
- 未激活失败只清理原身份候选，活动发布确认丢失是否保留全部结算来源？
- 原Root空库/损坏库字节是否保持，不把rollback当作数据已健康？
- 回退决定是否耐久且不能中途改为complete？
- Result存在但Root丢失/损坏时是否保留保护，而已完成重复请求是否不覆盖新状态？
- 父取消及重复取消是否等待唯一原线程结算后释放Owner？
- 静默探测是否关闭SQLite连接后再进行Windows Rename，是否错误声称隔离不合作Writer？
- Windows skip、独立macOS检出和Scripted Provider是否被误计为三平台或真实质量？

## 4. 证据复核与复验

[报告](README.md)、[结构化验证](verification.json)、[合同事实](contract-facts.json)、
[实际制品](wheel-observation.json)及[资料Manifest](bundle-manifest.json)统一集中于本目录。
先核验精确成员与SHA，再固定源码运行对应受管选集；测试组重叠不累加。
12项API缺失RED与2项候选失败RED均来自未提交前置状态，不能冒充旧固定源码的已复现漏洞。
两个Python版本都是macOS ARM64，实际Wheel仍为0.1.0验证制品，未构建sdist。

## 5. 后继发布门禁

R1整体装配安全、权利/许可、真实编码质量、Windows/三平台发行、独立Beta及最终封板仍开放。
不因本切片恢复可用而关闭这些门禁，不增加远程服务、自动更新、通用清理或跨机迁移平台。
