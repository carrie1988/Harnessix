---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product_config, delivery, models, agent]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_prefix_task_owner.py
  - tests/product_config/test_git_prepared_link_ledger.py
  - tests/models/test_unknown_tool_boundaries.py
supersedes: []
---

# R3与R4任务边界评审包

- **接受的事实**：R4准确Task准入与只读来源观察分层；消费回调前后原控制/来源复核；389项原语及边界回归、747项模型及原预算回归通过，两个集合不合计。
- **独立审查**：定位并修复通用SQL窗口降级及正常返回回调撤销context两项实际反例；最终差异静态审查未发现新反例。静态审查不替代测试。
- **消费者事实**：同一最终Wheel的实际认证SDK非空重开与四类原异常身份两项回归2/2通过；完整消费者矩阵不据此关闭，候选绑定以[facts.json](facts.json)为准。
- **明确拒绝的解释**：Task等于锁、路径等于FD、原语等于整个B7、录制Wire等于真实编码质量、内部候选等于1.0商用发布。
- **R3状态**：只增加边界测试与待决详设；未知Wire名称不透传、不模糊映射、不生成审批或新执行能力。
- **失败保留**：原8项连接负控失败、单文件原型冲突、SQL降级负控、撤销context负控及环境失败不删除或重解释为通过。
- **范围**：只推进R3/R4，不启用Git Writer、不修改模型费用账本、质量分母、原业务代码或外来未跟踪目录。
- **入口**：[完整报告](README.md)、[机器验证](verification.json)、[公开Manifest](manifest.json)、[R4详设](../../changes/m09-r4-git-connection-ownership.md)、[R3详设](../../changes/m09-r3-unknown-tool-recovery.md)。
