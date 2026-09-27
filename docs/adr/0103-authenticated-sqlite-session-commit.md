---
doc_type: adr
status: draft
version: 1
code_revision: 24e4e9c987055fe3a1590d3d0eada8f60574efa7
owners: [core]
modules: [session, agent, artifacts, secrets]
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
related_tests:
  - tests/agent/test_authenticated_store.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# ADR-0103：认证SQLite新事实与派生投影

## 背景与决策

Event Seal是候选，必须由真正新事件CAS、原字节持久化与同事务证明建立可信事实。
在原SQLiteSessionStore显式装配SessionPublicationBinding；新增空sidecar结构与独立Key认证逻辑头，
拒绝Enrollment旧未证明数据，不补签原历史。完整原Seal字节构成前缀链，Checkpoint MAC认证链头、
原投影、序号和累计字节；读写共享资源预算，不提交永久不可重放事实。

## 取舍与边界

不复制另一套Store，不引入HTTP/Worker，不放宽Artifact Epoch；通过内部端口分离原追加与投影SQL，
结构阈值不变。Snapshot来源认证不替代全历史重放审计和当前材料公开检查；
不认证物理路径/Tenant、不防整Thread/证明删除或有效整库回滚。
默认Root的Key Backend、Artifact正文跨重启和备份迁移不被显式库合同替代。

## 验证与后续

真实SQLite、CAS、Runtime/Fork/Artifact混合事务、取消/资源/响应丢失/真实进程退出均有测试。
[完整设计](../changes/m09-4a-authenticated-sqlite-session.md)给出状态、字段、源文件和失败矩阵。
本ADR为全链评审草案；0.9.4a及发布不关闭。
