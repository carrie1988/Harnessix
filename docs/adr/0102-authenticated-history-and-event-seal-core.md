---
doc_type: adr
status: draft
version: 1
code_revision: 829dabf8b7051b1242de2202ca9b1df99dc015bc
owners: [core]
modules: [session, agent, secrets, artifacts, app_server, product_config]
related_adrs:
  - docs/adr/0102-authenticated-history-and-event-seal-core.md
related_tests:
  - tests/session/test_publication_seal.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# ADR-0102：认证历史边界与Event Seal核心

## 背景与目标

当前Scope不能识别未登记旧值；普通SHA不能认证来源；随机Artifact Epoch不能跨实例复用。
需要把保护完成事实、入库成员身份、原正文完整性和当前公开授权分开设计。

## 决策

采用独立256位密钥、HMAC-SHA256、版本化域分离和完整原事件字节摘要。
声明绑定Key/逻辑Store/Thread/Event/Sequence/Schema/原Scope快照身份及版本元数据摘要。
核心返回候选正文与Seal，只有确认新事件身份和CAS的同一数据库事务可提交；旧行不自动签发。
认证验证不等于当前公开许可；当前Scope仍检查协议、工具、模型与Artifact出口。

## 取舍

否决普通SHA作为认证、模型API Key派生密钥、随机Epoch当永久证明、扫描当前值后补签旧历史。
只靠逐事件签名不覆盖派生投影、Fork来源、完整链或整库回滚，后续必须实现对应合同。
密钥本机自动托管与显式管理尚待产品策略决策；默认Root不接入临时密钥，不以原型密钥文件冒充正式托管。

## 状态、验证与后续

本ADR的总体方案为评审草案；EventPublicationAuthority、闭合Seal DTO和Scope元数据能力已实现并测试。
SQLite提交/读取、投影链、Artifact持久证明、默认装配、三平台Key Backend和备份迁移尚未完成。
[完整设计](../changes/m09-4a-authenticated-history-and-seal.md)、[源码研究](../research/authenticated-history-and-seal.md)与
[核心证据](../validation/event-seal-core-2026-09-28-v1/README.md)不得作为0.9.4a或产品发布完成声明。
