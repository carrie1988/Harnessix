---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, artifacts, trusted_actions, models]
related_adrs:
  - docs/adr/0163-artifact-archives-are-optional-when-storage-is-full.md
  - docs/adr/0026-transactional-artifacts.md
related_tests:
  - tests/agent/test_tool_result_protection_limit.py
  - tests/product_config/test_protection_limit_per_call.py
  - tests/models/test_tool_result_text.py
  - tests/trusted_actions/test_publication_recovery.py
supersedes: []
---

# ADR 0174 保护检查做不完时只让那一次调用失败

- 状态：评审中
- 日期：2026-11-18
- 修订：Agent“工具结果的公开保护拒绝使Turn失败”；Artifact与Trusted Action公开投影中`artifact_omitted`的取值（ADR 0163只有`storage_full`）

## 背景

工具结果、完整存档和审批Review在公开前都要经过Secret保护检查。检查有资源上限（字节1 MiB、节点10256、深度64、
工作量64 MiB）和10秒期限，任何一项用完都报`public_output_limit`或`public_output_timeout`，此前与发现Secret一样使整个
Turn失败。

Beta Issue #37（0.10.0b50 Windows）：长凭据作用域下一次普通grep的完整存档超过工作量上限，整轮提前终止。0.10.0b51按实际
比较量计数修复了这个触发条件（secrets.md第9版）。报告人随后补充的Fastify #7113会话暴露了遗留问题：grep与一条需要审批、
尚未准备的宿主测试命令在同一步，grep检查超限后两者都记为`uncertain_effect`，Turn以`interrupted`结束、禁止自动重放，
评测者只能人工核对命令到底有没有启动。检查超限并不是发现了Secret，正文不公开就已安全，没有理由让整轮和同批调用承担后果。
负责人2026-11-18决定：改成只让那一次工具调用失败。

## 决策

1. **范围。** “检查做不完”指`public_output_limit`与`public_output_timeout`
   （[`agent.publication.PROTECTION_LIMIT_FAILURES`](../../src/harnessix/agent/publication.py)）。发现Secret、Secret不可用、
   保护能力缺失、正文无效等其他拒绝不变。
2. **只读结果的完整存档。** [`SQLiteArtifactStore.publish`](../../src/harnessix/artifacts/sqlite.py)在插入前检查存档正文；
   做不完时不插入存档，提交`{"preview": <已通过检查的预览>, "artifact_omitted": "protection_limit"}`，与存储已满
   （ADR 0163）同一形状，Turn继续。
3. **只读结果本身。** [`protect_tool_result`](../../src/harnessix/agent/tool_result_protection.py)：调用为只读且结果不带
   任何效果元数据时，检查做不完就换成这一次调用的`failed`结果，错误码沿用检查码，英文说明请模型缩小请求后重试；
   原正文不公开。同批其他调用照常执行和记录。带效果的结果照旧失败关闭。
4. **命令输出。** [`archive_action_output`](../../src/harnessix/artifacts/action_output_store.py)遇到`public_output_limit`时
   返回`{"artifact_omitted": "protection_limit"}`；结果按Router审计终态如实提交（成功、非零退出等），
   [`validate_public_projection`](../../src/harnessix/trusted_actions/public_outcomes.py)接受两种原因。存档检查超时时，
   输出投影共用的10秒期限（`DEFAULT_OUTPUT_BUDGET`）也已用完，仍按原样失败。
5. **审批前的Review。** Review阶段保留这两个码（`public_errors`）；
   [`preparation_rejection_result`](../../src/harnessix/trusted_actions/preparation_rejection.py)与Review存不下时一样，由
   `system.validation`拒绝并关闭原Route，结果为`failed`，说明动作没有执行、什么都没改，建议拆成更小的动作；不发审批请求。
6. **告诉模型。** 截断处写“full results/output not saved (too large to check for protected values); narrow the request to
   see the rest”，不给不存在的`artifact_id`；命令非零退出的英文说明给出对应原因。

## 没有改变的

- 上限值、扫描覆盖与检查位置；检查做得完时行为与输出逐字节不变。
- 发现Secret仍使整轮失败（`public_output_secret_leak`）。
- 带效果结果的内联检查做不完（内联结果受输出预算约束，实际极少）照旧失败关闭；恢复路径“效果已验真时只补元数据”不变。
- Turn因其他原因失败时，同批尚未开始的调用仍按原规则收尾（另立决策项）。
- 持久形状：只给`artifact_omitted`增加一个取值；事件、Seal与重放不变。

## 后果

- #37的情形：搜索返回已通过检查的预览并注明完整结果未保存，旁边的命令照常进入审批、执行，Turn完成。
- 新增公开取值`artifact_omitted: "protection_limit"`，客户端与`storage_full`一样显示为“完整输出未保存”。
- 三个平台行为相同：判断在Python层与SQLite事务内完成，没有平台分支。

## 验证

0.10.0b55上失败、本版本通过：

- `tests/agent/test_tool_result_protection_limit.py`（真实`SecretPublicationScope`）：grep完整存档超过工作量上限时保留预览、
  带`protection_limit`、无存档行，同一步的`read_file`成功，Turn完成（此前Turn以`public_output_limit`失败）；结果本身检查
  超限或超时时只有该次调用失败且不含原文（此前Turn失败）；结果里有受保护值时Turn仍以`public_output_secret_leak`失败。
- `tests/product_config/test_protection_limit_per_call.py`（真实产品装配）：grep与需审批的测试命令同一步，grep存档检查超限后
  命令照常审批并执行成功，Turn完成（此前grep为`failed`、命令为`unknown`，Turn以`uncertain_effect`中断，与报告一致）；
  命令已执行而完整输出检查超限时结果为成功、带`protection_limit`、审计状态succeeded（此前Turn失败）；改动Review检查超限时
  Route被拒绝、未交给Executor、文件不变、Turn完成（此前`uncertain_effect`中断）。
- `tests/trusted_actions/test_publication_recovery.py`：Review阶段只保留这两个码，其他公开保护拒绝码仍不越出输出阶段，消息一律重建。
