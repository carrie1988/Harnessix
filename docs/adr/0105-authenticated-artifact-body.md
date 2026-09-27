---
doc_type: adr
status: draft
version: 1
code_revision: 33a2fd25bf6f529d1019cf584e02673734369299
owners: [core]
modules: [artifacts, session, secrets]
related_adrs:
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/artifacts/test_authenticated_body.py
  - tests/agent/test_authenticated_store.py
supersedes: []
---

# ADR-0105：Artifact原正文使用独立Session Key持久认证

## 背景与决策

运行Epoch只能拒绝其他进程读原正文，不能让已安全发布的Artifact在合法重启后恢复。
普通SHA和当前材料重扫不能证明原始发布安全。默认产品已拥有独立持久Session Key。
因此在Migration 0030给Artifact行新增可空Seal；新Artifact原行完成持久前保护后，
使用独立用途域HMAC-SHA256签原行；Tool/Batch与Session引用同事务，
Review/Output先在同一Artifact事务暂存正文及Seal，待后续Session反向引用才可公开。

签名包括逻辑Store/Key ID、Artifact/Thread/Turn/Call ID、Workspace Scope、用途、
原Epoch/政策、原Manifest及正文摘要、大小、原时间字段以及原冻结Scope摘要。
读取先验原来源，再沿既有Session引用、TTL、当前Workspace与当前Secret Scope验证；
Seal通过不授予读取、审批、效果执行或模型出站权限。

## 选型与取舍

- 不把Epoch升级为跨重启凭据；Epoch仍用于无Key宿主的旧库兼容分支。
- 不生成新Artifact密钥文件、不复用模型API Key，不引入Action Plane服务。
- 单行同事务Seal避免正文/证明双存储原子性缺口；迁移不回写旧行、不自动补签。
- 证明目的域与Session Event/Projection域分离；用途字段闭合，不允许跨对象复用MAC。
- 原始读取采用SQLite`substr`上限加一；空BLOB显式保留，超限返回失败而非截断后认证。
- 过期Tombstone保留原Seal作为历史字节，但状态/正文不可公开；Key丢失时不降级为Epoch。

## 后果、未覆盖项与验收边界

已认证新正文可跨Root重启与Secret轮换读取，但轮换后的当前材料若命中旧正文，仍失败关闭。
旧无Seal正文在认证Root仍拒绝；需单独设计有权重建或隔离导入，不静默追认。
不防掌握Key的同UID代码、管理员、合法完整库回滚、整组删除；不加密SQLite正文。
Action各生产者的真实三平台重试/崩溃和Windows原生Key仍须独立验收，
0.9.4a、0.9.5、0.9.6及许可证门禁未因本决策完成。

[总体/详细设计](../changes/m09-4a-authenticated-artifact-body.md)定义字段、流程、失败语义和源码映射；
[研究](../research/authenticated-artifact-body.md)区分参考源码事实与本项目独立安全决策。
