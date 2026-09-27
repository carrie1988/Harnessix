---
doc_type: adr
status: current
version: 1
code_revision: 3c5f6e36d9c9ce98709c5c37ae2316709c443001
owners: [core]
modules: [agent, artifacts, secrets, product_config, session, trusted_actions]
related_adrs:
  - docs/adr/0095-versioned-secret-publication-scope.md
related_tests:
  - tests/agent/test_publication_runtime.py
  - tests/product_config/test_publication_scope.py
  - tests/artifacts/test_publication_upgrade.py
supersedes: []
---

# ADR-0096：产品模型原凭据与当前运行Artifact公开证明

## 状态

接受当前运行的公开保护切片；历史Session治理与跨重启正文安全恢复未完成，不作为整体生产发布完成结论。

## 背景与决策驱动

正式模型凭据可在只读完整Artifact中出现，预览无值而SDK页与后续历史包含值。
Process Secret绑定Scope不自动包含模型凭据；仅结果合同与Hash验证不能证明值安全。
[完整详细设计](../changes/m09-4a-product-publication-boundary.md)描述源码研究、四类生产路径、纯接口、
字段、四种图、伪代码、错误、取消、期限、事务、真实升级与开放范围。

## 决策

在模型工厂前捕获所选Profile链原材料，同一Scope用于模型构造与公开检查；模型引用只含name/version，不伪造Process注入target。
Agent以纯PublicOutputProtection端口检查ToolResult；完整JSONL使用原字节、唯一键解析、原生树和规范JSON共用预算。
Store首次写入正文前检查，并在原事务新增当前随机Epoch与固定policy；分页、单件与批量历史验证再核验证明及全文。
原body、Manifest、Hash、TTL和公开Tool合同不改。所有未知检查异常固定归一，取消继续传播，原已确认效果不重执行。

## 备选及后果

拒绝只检查页或预览、发布时重读环境、保存原凭据、事后改写正文/Hash、同版本追认旧正文和无限内存证明缓存。
Migration 0028只增加可空内部证明列；旧行不重签。默认产品启用保护，无保护独立宿主仅保持旧兼容行为。
当前Store重建会拒绝旧Artifact正文，明确影响跨重启长会话与Fork可用性；这不是可恢复发布验收。
Epoch不是密码学Seal或对数据库管理员的防篡改证明，全文复验与原归属/Hash检查继续执行。
后续必须单独设计旧材料证明与历史公开治理，不能以保守拒绝替代可恢复、可审计产品总体目标。
