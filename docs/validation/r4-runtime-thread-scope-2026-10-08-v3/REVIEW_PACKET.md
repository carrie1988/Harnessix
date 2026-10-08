---
doc_type: validation-evidence
status: current
version: 3
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/product_config/test_git_prepared_link_terminal_callbacks.py
  - tests/product_config/test_git_link_user_observation_consumption.py
supersedes:
  - docs/validation/r4-runtime-thread-scope-2026-10-08-v2/README.md
---

# Git Thread 消费者终态复核包

## 准入判断

只接受三完整文件 49 项功能证据；不关闭完整产品、性能、真实业务和商用发布门禁。

## 必查

1. JUnit 包含 controls 10、terminal callbacks 3、U consumption 36，无删项或跳过。
2. 候选 Wheel 与已冻结 556 个生产成员一致，无运行期间源码或输入变更。
3. 5200.738 秒长耗时不能误称性能达标；原并行期限失败保留。
4. cProfile 在原进程结束后独立运行，最多 240 秒，仪器影响、截断与首异常如实记录。
5. 诊断计数不是新的功能成绩，41 history／125 ports 不经完整复验不得拼接为全绿。
6. 全集认证、取消、期限、回调与事务边界未降低，默认 Writer 仍未启用。
7. v1／v2 公开与私有原件不变，追加文件由新 manifest 绑定。

## 复核索引

按 README、facts、verification、manifest 复算结果及原件摘要。
研发组件通过、响应性、三平台实际编码、Beta 与商用退出属于不同层级。
