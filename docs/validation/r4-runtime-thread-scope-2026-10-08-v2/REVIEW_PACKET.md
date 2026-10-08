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

# Git Thread 接线追加复核包

## 范围与结论

仅接受两项同候选实际 SDK 功能复核；完整回归、性能、R3／R4 和商用发布不因此完成。

## 必查

1. 二项 JUnit 结果、逐项名称、时间、候选身份及生产成员一致性。
2. 原 14 项失败保留，纯替身修订与实际审批期限失败分别解释。
3. 原期限、认证全集、首失败、取消和事务边界未降低。
4. 当前仍在执行的 consumer 不纳入终态成绩；短系统采样不当作正式性能基线。
5. v1 公开成员及私有封存摘要保持，v2 为追加记录而非追写历史。
6. 默认 Writer 与完整 B4／B7／P1 保持开放，没有新增模型请求或客户工程验收。

## 原件核验

按 README、facts、verification 与 manifest 核验公开及私有原件。
同名版本号不证明相同 Wheel，不把两个子集相加解释为完整回归通过。
