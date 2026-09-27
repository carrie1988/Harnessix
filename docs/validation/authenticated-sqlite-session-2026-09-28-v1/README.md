---
doc_type: validation-evidence
status: draft
version: 1
code_revision: 857ce38444d89fef69a860946f92764c6d3adf9f
owners: [core]
modules: [session, agent, artifacts, secrets]
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
related_tests:
  - tests/agent/test_authenticated_store.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 认证SQLite Session验证报告

## 1. 当前范围与结论

真实SQLite新CAS、原Event/Seal/前缀和Snapshot/Checkpoint同事务正式库合同已实现。
默认产品Root、正式持久Key Backend和Artifact正文/二进制跨重启认证未完成，
不得据此关闭0.9.4a、0.9或正式发行。真实模型请求0次；12件Archive权利继续阻塞发布。

## 2. 六文件证据与设计

[合同事实](contract-facts.json)、[验证](verification.json)、[Manifest](bundle-manifest.json)、
[Review Packet](review-packet.json)和[CI状态](ci-observation.json)固定Revision、摘要、输入和验收边界。
[总体与详细设计](../../changes/m09-4a-authenticated-sqlite-session.md)包含四幅实际渲染并逐图检查的图、
字段、原事务链、伪代码、异常/取消/恢复/迁移边界和固定源码符号导航。
[源码研究](../../research/authenticated-sqlite-session.md)与[ADR](../../adr/0103-authenticated-sqlite-session-commit.md)说明选型与取舍。

## 3. 已执行验证

- 42项实际SQLite测试覆盖篡改、原字节、跨Scope重开、幂等、并发CAS、旧历史拒绝、
  父任务取消、Scope关闭、限额、响应丢失、真实OS退出及Runtime/Fork/Artifact混合事务。
- 原46项Seal核心与上述42项共同88项通过；相关1405项通过。各组重叠，不相加。
- 固定Git归档构建Wheel/sdist；两个独立`python -I`进程从候选Wheel消费实际生产Store合同，
  验证原字节、四类事实共同提交、异进程/新Scope重开、投影及普通SHA替换拒绝、原事件重建和无保护重开拒绝。
- 持久Key文件是独立消费者fixture，不是产品Key Backend；依赖复用本机环境，
  不声明干净机器安装、可复现构建、正式发行物或三平台安装验收。
- 707个既有验证文件原字节保留。未跟踪安全草稿未读、未运行、未打包、未纳入通过计数。
- Snapshot证明是可信Reducer的原派生事实，不逐次重扫所有Event正文；
  events/rebuild/Fork完整前缀另行认证。未证明旧数据拒绝激活，不重签、不追认、不删除。

## 4. 待冻结验证与发布边界

完整回归尚待执行，本报告为草案；不会把未运行验证作为通过。
CI在批量推送后后台运行，不逐提交等待；当前本机验证不代表Linux/macOS/Windows CI已通过。
已为macOS/Windows添加Session核心与实际认证Store测试入口，正式三平台状态按精确Revision另行记录。
产品密钥托管、Artifact正文来源认证、Owner/全部Provider/SDK相关ID、远端MCP、
来源权利、真实安装与Provider成本继续按路线图推进。
