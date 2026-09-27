---
doc_type: adr
status: current
version: 1
code_revision: 648f5f1b7b422462979f25df036994802f0b553f
owners: [core]
modules: [secrets, trusted_actions, product_config, mcp]
related_adrs:
  - docs/adr/0094-audit-bound-bounded-owner-projection.md
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
related_tests:
  - tests/secrets/test_provider.py
  - tests/product_config/test_process_action.py
  - tests/mcp/test_server.py
supersedes: []
---

# ADR-0095：执行与公开复用同一版本化Secret内存快照

## 状态

接受；仅关闭本详设明确的当前值公开边界，不关闭整体0.9.4a。

## 背景与决策驱动

真实Runtime证明字段合同不能阻止合法字符串中的正式绑定Secret值。
[完整详细设计](../changes/m09-4a-versioned-secret-publication.md)给出源码研究、接口、流程、
预算、故障、恢复、安全及验收；局部Debug/正则保护不能证明JSON公开安全。

## 决策

宿主只捕获显式name/version绑定，同一SecretPublicationScope既作为执行Provider又检查公开
原生副本。已有有限模式复用，加入共享节点/字节/工作量预算及原投影取消/期限。
缺能力和命中默认拒绝，公开消息固定，不修改已确认Hash或再执行。
恢复只有Hash的Secret Owner正文无原值证明，调用Owner前拒绝；效果元数据仍可恢复。
产品对已验证Profile显式装配和关闭，独立MCP出口显式检查，不增加Action服务或持久明文。

## 备选及后果

拒绝重新读取当前环境（旋转风险）、无限全环境扫描（隐含权限）、事后改写正文（Hash冲突）
和以版本相同追认旧正文（缺少旧值证明）。保留原Executor内部持久化前脱敏。
不关闭全部模型凭据、历史Session/Artifact治理或跨重启Secret正文恢复；不得把保守拒绝
冒充这些能力已经完成。Python清零仅尽力处理可变副本，宿主阻塞不能被同步检查点硬中断。
