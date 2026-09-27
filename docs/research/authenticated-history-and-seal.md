---
doc_type: source-research
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

# 历史授权与跨重启证明源码研究

## 1. 需求与研究边界

目标是证明历史数据确实经过当时完整保护，且身份、版本、正文和当前消费关系未被替换。
当前材料扫描、无密钥SHA和随机运行Epoch分别处理不同问题，不能互相替代。
本研究关注实际持久入口、恢复读取、Artifact与备份；不推断上游项目所有安全能力，也不复制非官方来源实现。

## 2. 固定上游源码与设计动机

- Codex固定`a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`的
  [Rollout写入](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/rollout/src/recorder.rs#L1970-L1996)
  将时间、序号与语义Item编码成JSONL并flush。可借鉴持久事实与投影分离；该写入片段不能证明本产品的历史保护授权。
- OpenCode固定`69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`的
  [Session身份Schema](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/session/schema.ts#L1-L26)
  为Session/Message/Part提供类型身份。类型身份有助于绑定资源，但不等于正文来源认证。
- 本地Claude Code非官方快照固定`2ca5ddabfed5f220812ea11f029eda03b21bc4c1`，
  `src/assistant/sessionHistory.ts:26–44`显式组装HistoryAuthCtx并复用到分页请求，体现历史访问授权与分页读取分离。
  该文件没有可验证的官方开源来源链接，不作为来源权利证明、官方接口合同或可复制实现；仅进行职责对比。

## 3. 本产品实际调用链

1. [`AgentRuntime._commit`](../../src/harnessix/agent/runtime.py)保护单次payload，但其他生命周期方法和Artifact调用也可追加事件；仅在Runtime包装层加证明会遗漏入口。
2. [`SQLiteSessionStore._append_in_transaction`](../../src/harnessix/session/sqlite.py)统一CAS、事件写入和投影；Artifact混合事务也调用这一入口。
3. `_snapshot`只检查当前投影SHA与事件数量/最大序号，不认证写入者；`_scan_recovery_threads`还有独立批量路径。
4. `_events`/`_validated_replay`/`rebuild`直接消费事件正文；新证明必须在反序列化/模型请求之前生效。
5. [`ArtifactPublicationGuard`](../../src/harnessix/artifacts/publication.py)使用运行Epoch，不能直接作为持久密钥证明。
6. [`维护备份`](../../src/harnessix/session/maintenance_backup.py)复制数据库并验证SQLite完整性及计划身份；当前没有保护密钥的备份/迁移关系。

## 4. 固定基线真实观察

干净Git归档中的实际默认Root装配Config、Scope、SQLite、默认Action与只读宿主；仅模型工厂/stdio驱动替身。
五个直接Service查询均可返回未登记旧材料，原历史不变、后台Task和当前Provider请求为0。
另一个实际SQLite观察同时替换snapshot_json和普通SHA，事件正文不变；get_thread接受替换投影。
此攻击需要数据库写权限，不证明正常Workspace具备访问状态目录的权限。

[版本绑定证据](../validation/event-seal-core-2026-09-28-v1/contract-facts.json)记录具体布尔结果、表列与脚本Hash，不存原材料或正文。
既有随机Epoch仍故意拒绝跨Store实例正文，不能把放宽Epoch或换成路径Hash当作恢复整改。

## 5. 结论与尚待实施的完整范围

需要独立密钥、可信原保护完成证明、逻辑Store身份、同事务提交、认证事件链及派生投影证明。
签发候选不证明该事件已经入库，更不证明它曾在过去得到授权；历史迁移不得调用新事件签发器追认旧行。
新Event Seal核心只是前置正式组件，当前默认Root、历史读取、Artifact与密钥备份均尚未接入。
完整目标、数据合同、失败与恢复矩阵见[总体与详细设计](../changes/m09-4a-authenticated-history-and-seal.md)。
