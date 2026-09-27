---
doc_type: adr
status: draft
version: 1
code_revision: 735f2e9dcafbe863e36a2890fac1364c4283bca8
owners: [core]
modules: [agent, app_server, protocol, secrets]
related_adrs:
  - docs/adr/0099-input-persistence-and-command-publication.md
related_tests:
  - tests/app_server/test_frame_publication.py
  - tests/product_config/test_protocol_publication_cli.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# ADR-0100：原封套准入、完整响应字节与纯握手候选

## 背景

类型化命令保护不能覆盖Protocol原id、未知键的校验path、初始化元数据和只读Replay。
固定基线真实Runtime/SQLite/Server探针确认当前材料通过这些路径回显，出站也未落实协商字节限额。

## 决策

Codec后先检查原相关id和完整封套，再分派；完整原响应字节先执行协商长度检查，再复用纯公开保护端口。
initialize返回内部不可变候选，保护通过后复核NEW/关闭状态，无await提交；并发只有一个胜者。
关闭后合法Notification不响应。失败返回有限固定控制错误，不递归依赖已失效保护器，不回显未检查字段。

## 备选与取舍

不清洗原id、不改变Hash、不分散实现多个扫描器，不提前提交握手状态。
按实际UTF8含换行计数；超限返回response_too_large而非截断或静默重执行。
出口失败保留原completed领域事实。当前材料检查不授予旧版本历史授权，直接Service查询仍是独立出口。

## 结果、失败与验证

原Schema/数据库不变，纯嵌入兼容；失效Scope拒绝请求，严格SDK的相关id可用性仍开放。
61项帧/握手测试中含1项开放旧历史观察，1项实际产品CLI管道测试，另有既有回归和证据治理门禁。
完整接口、字段、五图、源码导航、恢复与部署见[详细设计](../changes/m09-4a-protocol-frame-publication.md)。
[验证证据](../validation/protocol-frame-publication-2026-09-28-v1/README.md)不授权整个0.9发布，12项Archive权利继续阻塞。
