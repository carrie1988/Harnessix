---
doc_type: source-research
status: draft
version: 1
code_revision: 33a2fd25bf6f529d1019cf584e02673734369299
owners: [core]
modules: [artifacts, session, secrets]
related_adrs:
  - docs/adr/0105-authenticated-artifact-body.md
related_tests:
  - tests/artifacts/test_authenticated_body.py
  - tests/artifacts/test_publication_upgrade.py
supersedes: []
---

# Artifact正文持久来源认证源码研究

## 1. 固定来源与适用范围

研究访问日期：2026-09-28。参考仓库均为本地固定提交，不以仓库命名推断认证能力。

| 来源 | 固定提交与符号 | 已核实事实 | 本项目取舍 |
|---|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`，`codex-rs/rollout/src/recorder.rs`的`RolloutRecorder` | 会话事实写成可重放JSONL，写入与读取由独立模块组织 | 借鉴事实与消费分层；JSONL本身不证明Artifact原正文安全或MAC，不能直接复用作为本证明 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`，`packages/opencode/src/session/message-v2.ts`的`MessageTable`/`PartTable`读取 | 消息与Part有独立持久表和视图转换 | 借鉴分层，不把当前模型视图重新计算当作历史来源证明 |
| Harnessix | [`persistence.insert_artifact`](../../src/harnessix/artifacts/persistence.py)、[`SQLiteArtifactStore`](../../src/harnessix/artifacts/sqlite.py) | Tool、Patch、Process等归档共用插入，Session引用与Artifact正文同事务；原Epoch跨重启不同 | 在共用插入点签原行、在全部读取入口先验签，再执行原反向Session授权与当前保护 |

逆向样本不是认证设计依据；本切片不复制或声称上述产品提供相同HMAC能力。
安全依据来自本项目明确的威胁模型、原源码调用链、形式化字段与故障回归。

## 2. 旧调用链与失败根因

1. [`ArtifactPublicationGuard`](../../src/harnessix/artifacts/publication.py)原持有`uuid4()`生成的运行Epoch；迁移0028保存Epoch和政策，但跨进程失效。
2. [`insert_artifact`](../../src/harnessix/artifacts/persistence.py)在INSERT前执行JSONL/类型化二进制保护；原行中没有独立密钥认证证明。
3. [`read`](../../src/harnessix/artifacts/sqlite.py)、`verify_reference`和[`batch_verify`](../../src/harnessix/artifacts/batch_verify.py)要求当前Epoch并重扫当前Scope。换新进程合法旧正文也被拒绝。
4. 普通SHA-256和Manifest可由SQLite写入者一起伪造；当前Scope不含某个旧Secret，并不能追认旧正文曾被保护。
5. 现有独立托管Session Key/Store ID/Key ID为稳定来源认证提供必要材料；Artifact无需另一个Action服务、模型凭据或第二套Key文件。

## 3. 决策前约束

- 旧Artifact原正文、Manifest、Epoch和Session事件必须逐字节保留；新增迁移只能给旧行留空证明。
- 正文及Seal同一SQLite事务；Tool/Batch引用同事务，Review/Output后续建立Session引用；确认丢失只查验原收据，不重签或刷新TTL。
- 原证明绑定完整原正文、原Manifest、身份、用途、Workspace Scope和发布时间；当前Secret Scope仍须在每次公开读取复验。
- 过期Tombstone合法清空正文；过期不得作为公开正文成功返回。
- 无保护独立库保留原兼容语义；默认Product Root始终装配独立Key和冻结Scope。
- SQLite恶意大BLOB在复制到Python之前限为上限加一；截断不成为合法正文。

总体及详细设计见[Artifact来源认证详设](../changes/m09-4a-authenticated-artifact-body.md)，
正式取舍见[ADR-0105](../adr/0105-authenticated-artifact-body.md)。
