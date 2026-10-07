---
doc_type: validation-evidence
status: current
version: 1
code_revision: ba6171f32e5575be727364d166001ca0619db6a0
owners: [core]
modules: [product_config, delivery, session]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
supersedes: []
---

# Git P1 研究复验评审包

- **接受**：默认关闭研究桥的3项真实离线认证SDK链、15项短测；原发布与只读阶段分别核对。
- **拒绝合并**：原callback可观察性/瞬时漂移时点/失败顺序合同未决；所有I/O时窗、完整SDK和资源门禁不完整。
- **性能限制**：读取18.14秒、心跳最大间隔5.25秒是有并行前提的样本，无本轮配对baseline或稳态SLA结论。
- **资源限制**：FD诊断污染不等于生产泄漏或PASS。原FAIL、原60/120秒规则及Writer拒绝保持。
- **证据入口**：[完整说明](README.md)、[事实](facts.json)、[正式合同](../../changes/m09-r4-git-prepared-link.md)。
