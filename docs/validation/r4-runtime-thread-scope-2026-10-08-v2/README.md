---
doc_type: validation-evidence
status: current
version: 2
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_approval_history.py
  - tests/product_config/test_git_decision_source_sdk.py
supersedes:
  - docs/validation/r4-runtime-thread-scope-2026-10-08-v1/README.md
---

# Git 原 Runtime Thread 接线补充复核报告

## 1. 结论与适用范围

原并行回归中失败的两项审批正控，在同一安装候选、原输入、原期限和原认证规则下独立复核：
**2 项通过，耗时 230.383 秒**。这说明所列原功能场景可完成，不说明并行响应性问题已经解决。
**R3、R4、B4、完整 B7 与 P1 仍开放，商用发布 NO-GO。**

本目录是追加证据版本。[v1 固定组件交付](../r4-runtime-thread-scope-2026-10-08-v1/README.md)
及其私有原件不覆写。v1 记录的复核执行中状态属于原快照时点，本目录记录后来获得的二项终态。

## 2. 候选、原件及源码位置

生产源码、Wheel、独立非 editable 安装与 Git 对象的 556 个成员字节一致。
代码提交、Wheel SHA、JUnit SHA 及逐项名称见 [facts.json](facts.json)。
运行环境为 Python 3.12.7、原 uv.lock 和 Git 2.53；不以相同版本字符串替代候选身份。
[十四节总体及详细设计](../../changes/m09-r4-git-runtime-thread-scope.md)给出架构、流程、
时序、数据、字段、接口、异常与持锁伪代码；本目录不重写或扩张该设计。

两个实际 SDK 消费者分别位于
[审批历史](../../../tests/product_config/test_git_prepared_approval_history.py)的
`test_original_router_first_window_rejected_then_original_sync_recovery` 和
[批准来源](../../../tests/product_config/test_git_decision_source_sdk.py)的
`test_corrected_one_approved_read_then_non_target_mac_rejected`。
它们消费原 prepared Ledger 与 Router，不表示默认产品已经注册 Git Writer。

## 3. 验证结果与失败解释

| 证据 | 结果及解释边界 |
|---|---|
| 原完整 history 回归 | 27 PASS、14 FAIL；其中 12 项旧替身入口错误及 2 项实际审批期限失败；原件保留 |
| 现行决定单位完整集合 | 31 PASS；已在 v1 绑定；纯替身返回检查不当作真实来源认证 |
| 二项实际审批独立复核 | 2 PASS、0 FAIL；逐项约 115 秒；不能累加为新的独立业务任务 |
| 扩展 consumer 完整集合 | 尚未获得终态；进度点、日志暂时无失败或进程在运行都不是通过 |

原期限失败出现在同步末端 live-request 核验，观察到原 Turn 窗口已到期。
失败曾与多个实际 SDK 回归进程同时执行；独立复核通过是不同调度条件的功能证据，
不能据此将原并行失败自动解释为无效，也不能宣布性能 SLA 达标。
未扩大 60 秒消费期限或 120 秒 Turn 窗口，未延续 TTL、移除否定用例、减少全集认证或修改失败顺序。

## 4. 响应性诊断与后续验证

对扩展回归原进程进行 3 秒系统采样，保留原始采样与命令日志。
采样可见 Python 执行、SQLite 打开／准备与文件系统操作，但没有 Python 业务符号的完整归因。
该短采样不证明整体瓶颈比例、单调用复杂度或性能修复，也不作为正式 SLA 成绩。

P1 后续必须使用冻结候选的实际调用计数、相同输入的独立复验与完整边界负控定位，
保持回调次数、认证覆盖、首失败、取消、超时和事务可见性。
不得通过放宽期限、整体加锁或仅降低测试并发关闭 P1。
诊断不改运行中的测试输入或生产候选，不向正在执行的原件追写快照。

## 5. 安全、费用与未完成项

模型请求新增零、Beta 接受新增零，无新 Schema、数据库迁移、执行权限、后台服务或配置开关。
不读取凭据、客户工程或原费用账本，不触碰历史封存；主仓非任务文件不参与读取与验收。
R3 仍需有效费用授权登记及新冻结候选下的完整真实 Trial。
R4 尚需实际 SQLite FD、全部 dispatch、B4 末端一致性、P1 响应性、决定 Writer、
默认 Checkpoint／本地 Commit、A／T2／NativeBridge／D、Backup2 与三平台完整编码。

## 6. 复核与质量门禁

[Review Packet](REVIEW_PACKET.md)、[结构化事实](facts.json)、[质量核验](verification.json)
与 [成员清单](manifest.json)共同绑定本追加快照。
先验证公开成员与私有清单摘要，再复算 JUnit；不要把重复子集或不同测试输入修订后的结果拼成完整集合。
文档／Secret 完整门禁在追加文件齐备并纳入已跟踪扫描范围后执行，准确结果由核验文件记录。
三幅实际编译图沿用 v1，不宣称新增图形或三平台验收。
