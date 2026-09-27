---
doc_type: validation-evidence
status: draft
version: 1
code_revision: 6b5f5591d4dbe8cb31f99752b3e8950ae62a8bd0
owners: [core]
modules: [agent, app_server, protocol, secrets]
related_adrs:
  - docs/adr/0100-protocol-frame-and-handshake-publication.md
related_tests:
  - tests/app_server/test_frame_publication.py
  - tests/product_config/test_protocol_publication_cli.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 原协议帧与握手提交版本绑定验证报告

## 验证范围

原协议封套准入、完整UTF8响应帧限额与当前材料保护、握手候选提交和关闭通知。
当前专项161项通过，含62新增和99既有，其中1项为未登记旧历史开放观察，不能计为历史授权验收。
全量、固定Git发行物、五图渲染及证据Hash将在冻结前验证，草案不作发布验收。

## 固定证据与限制

[contract-facts](contract-facts.json)记录独立旧版负例和同脚本修复观察；
[verification](verification.json)记录命令与门禁；[Manifest](bundle-manifest.json)绑定原字节及来源；
[Review Packet](review-packet.json)保持发布阻塞；[CI观察](ci-observation.json)不混淆其他修订。
完整实现与五图见[详细设计](../../changes/m09-4a-protocol-frame-publication.md)。
真实CLI使用实际ProductConfig/Scope/SQLite与OS管道，合成材料，不接受Turn，不调用真实网络模型。
旧未登记历史、直接Service查询、全部Provider、跨重启Seal和12项Archive权利仍开放；整个0.9不标记完成。

## 已完成独立核验

相关矩阵2169项通过；专项161项通过。清洁固定Git归档构建Wheel与sdist，发布物Secret扫描通过。
隔离模式`python -I`直接从压缩Wheel导入新模块，实际Runtime/SQLite/SDK验证敏感id/键拒绝、
当前登记材料的旧Replay拒绝和安全原文往返；不是干净机器安装验收，不使用项目源码或测试助手。
完整测试尚待运行，当前草案不作全量通过声明。
