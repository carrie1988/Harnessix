---
doc_type: source-research
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

# 认证SQLite提交与派生恢复源码研究

## 实际调用链与缺口

原SQLiteSessionStore统一追加入口被普通Runtime与Artifact混合事务共同调用，不能只包Runtime._commit。
原_snapshot与批量_scan_recovery_threads分别解析投影；events/rebuild/Fork递归也消费原事件，须分别强制。
普通SHA与count/max不能证明原来源；只写原Event Seal不能认证巨大派生Snapshot或确认完整前缀。

## 设计证据与取舍

[前置固定研究](authenticated-history-and-seal.md)使用已固定Codex Rollout、OpenCode类型身份和
非官方本地Claude职责片段，明确可借鉴的事实/投影/访问授权分离，不据片段推断其全部安全性或源码权利。
本次沿实际公共入口和Artifact私有事务进行求证，使用已认证Checkpoint扩展可信Reducer，不重复扫描聚合后追认旧值。
严格Artifact UUID/时间合同要求JSON模式校验，Python原生JSON模式验证会误拒合法引用；真实Runtime归档链已验证该根因。
新迁移只增空结构；原1～28摘要及Transcript/Artifact不回写证据保持不变，当前迁移退出终点追加29。

## 当前结论与状态

显式Binding的SQLite新事实/投影/恢复已接入；默认产品Key托管与强制Root、
Artifact正文/二进制持久证明、备份迁移与三平台正式安装仍开放。
完整数据与流程见[详细设计](../changes/m09-4a-authenticated-sqlite-session.md)，不据局部测试声明整个0.9生产完成。
