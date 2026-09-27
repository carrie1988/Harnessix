---
doc_type: adr
status: current
version: 1
code_revision: 246b353337fbf9c9a625a2a310159fa8d4e04f51
owners: [core]
modules: [agent, app_server, protocol, session, secrets]
related_adrs:
  - docs/adr/0099-input-persistence-and-command-publication.md
  - docs/adr/0098-model-stream-publication-boundary.md
related_tests:
  - tests/agent/test_input_publication_runtime.py
  - tests/app_server/test_command_publication.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# ADR-0099：用户原输入持久前与命令原结果公开保护

## 背景、决策与源码依据

采用[完整详细设计](../changes/m09-4a-input-persistence-boundary.md)。已证明模型出站拒绝并不能防止用户输入先进入Session，
协议原request_id也可先进入命令账本。直接替换所有Store.append会破坏既有锁和先发生的审批审计语义。

1. 在Runtime原字段及有效TurnStarted/content前检查，在命令原参数Claim前检查；不改正文、身份、指纹和预算。
2. 同一纯公开保护端口复用原版本Scope，错误映射为有限public_input命名空间，不建立第二个扫描器。
3. 缓存completed/failed与本次原结果DTO检查先于回执公开；拒绝不改写已完成历史。
4. 问答五事件纯构造与命令持久生命周期内聚提取，保留原service错误导入、合同和事务顺序，不放宽结构门禁。
5. Trace与批准决定检查前移，直接cancel/排空保持独立；保护器丢失时SDK cancel拒绝是明确未合并的可用性边界。

## 接口设计、失败、兼容与验证

`protect_input`复用10秒检查期限、原字节/工作预算和父Task取消；输入命中不创建FAILED Turn，等待中的决定拒绝不产生审计。
字段、时序、伪代码、源码映射、部署与完整测试矩阵均见详设。无Schema/数据库迁移/新增服务或依赖环。
[固定证据目录](../validation/input-persistence-2026-09-28-v1/README.md)区分原版负例、整改、实际SDK/SQLite与模拟Provider。
原版资料不覆写；不把已知原始帧回显测试算成安全通过，也不以本地回归冒充真实三平台发布。

## 后果、风险与剩余范围

API会对包含已登记值的原输入返回稳定拒绝；纯嵌入没有保护器保持兼容但没有此安全保证。
历史未知版本、跨重启Seal、原始JSON-RPC id和错误path、全部Provider材料及整体发布仍独立开放。
12项依赖Archive权利审查继续阻塞正式发布，不将技术回归代替权利结论。
