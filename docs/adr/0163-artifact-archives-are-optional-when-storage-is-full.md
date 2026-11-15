---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [artifacts, trusted_actions, models, product_config]
related_adrs:
  - docs/adr/0164-expired-artifacts-are-reclaimed-automatically.md
  - docs/adr/0142-stale-approved-plan-fails-without-effect.md
related_tests:
  - tests/artifacts/test_store.py
  - tests/product_config/test_artifact_storage_full.py
  - tests/models/test_tool_result_text.py
supersedes: []
---

# ADR 0163 存储已满时结果照常提交、不带存档，不中断本轮

- 状态：评审中
- 日期：2026-11-15
- 修订：Artifact模块“标准只读结果要求正文与结果一起成功”；Trusted Action公开投影必须带`artifact`的合同

## 背景

搜索结果超过预览、命令输出和改动的审批Review都会把完整正文存为Artifact，存档受单Turn与工作区两级配额约束
（ADR 0164）。存不下时（`artifact_quota_exceeded`），0.10.0b46之前：

1. **只读搜索/列目录**：结果与存档同一事务回滚，整个Turn失败。
2. **命令输出**：命令已经执行，存档在执行之后发布；发布失败使恢复把结果记为`unknown`，整轮以`uncertain_effect`
   中断，而审计记录里命令其实已确定成功或失败，两边记录对不上。2026-11-15 NL2RepoBench冒烟中两个用例在第143、
   156步这样中断，前面的工作全部作废。
3. **改动的审批Review**：在审批之前发布，改动根本没有执行，整轮却同样以`uncertain_effect`中断，原Route停在待审批。

设计文档本来规定Batch/Process“准备、预算或配额失败时允许只提交原审批/结果事实”，代码只对未接入产品的Batch实现了。
配额是根治不了的硬墙：ADR 0164回收过期正文，但仍在TTL内的正文照样占名额。负责人2026-11-15批准降级而不中断。

## 决策

1. **只读结果。** [`SQLiteArtifactStore.publish`](../../src/harnessix/artifacts/sqlite.py)在同一写事务里判断配额：
   放得下时照旧插入存档并提交`{"preview", "artifact"}`；放不下时不插入存档，提交
   `{"preview": <原预览>, "artifact_omitted": "storage_full"}`，Turn继续。
2. **命令输出。** 两个命令Output Provider共用`archive_action_output`：存档发布失败于`artifact_quota_exceeded`时返回
   `{"artifact_omitted": "storage_full"}`代替`{"artifact": <引用>}`。结果按Router审计终态如实提交（成功、非零退出等），
   `trusted_action.artifact_sha256`仍是审计记录的正文摘要。恢复路径走同一Provider，结论一致。
3. **公开投影合同。** [`validate_public_projection`](../../src/harnessix/trusted_actions/public_outcomes.py)要求`artifact`与
   `artifact_omitted`恰有其一：前者仍须是匹配`artifact_sha256`的完整引用，后者只能是`storage_full`；两者都不计入
   `output_sha256`的摘要。其他字段与此前一样不得多出。
4. **审批Review。** Review存档存不下时，原Route由`system.validation`以`artifact_quota_exceeded`拒绝并关闭，Tool Result为
   `failed`，说明改动没有执行、什么都没变；不发审批请求，Turn继续。不降级为“没有Review照样审批”：用户批准时要看
   这份Review。
5. **告诉模型。** 模型线协议的文本渲染在截断处写“full results/output not saved (artifact storage full)”，不给不存在的
   `artifact_id`；命令非零退出的英文反馈改为建议缩小输出重新运行，而不是去读存档。未截断时预览就是全部内容，不提存档。

## 没有改变的

- 配额默认值、范围与检查位置；存得下时的行为与输出逐字节不变。
- 其他发布失败（正文无效、作用域或批准错配、输出预算超限等）照旧失败关闭。
- 历史引用核对：不带存档的结果没有引用，不参与核对，以内联方式进入模型上下文。

## 后果

- 存储满了以后长任务还能继续，代价是这部分结果没有完整存档：模型与客户端只能看到预览。
- 新增公开字段`artifact_omitted`（只读结果与命令输出），客户端应把它显示为“完整输出未保存”。
- 单Turn件数用尽后，同一Turn里的改动会逐个以`artifact_quota_exceeded`失败（Review存不下），命令与搜索不受影响。
- 不带存档的搜索结果在模型上下文里只能内联：搜索预览最多24 KiB（快照/正则48 KiB）记录，产品默认内联上限64 KiB，
  总能放下；把`max_inline_utf8_bytes`调到预览以下的部署，超限的无存档结果会在准备历史时失败，与没有Artifact存储时
  相同。命令输出的预览本来就不做替换。
- 三个平台行为相同：判断在SQLite事务与Python层完成，没有平台分支。

## 验证

0.10.0b46上失败、本版本通过：

- `tests/artifacts/test_store.py`：字节配额（单Turn、全局）下搜索结果带`artifact_omitted`提交、无存档行、Turn完成；件数配额
  下第一次存档、第二次省略；并发两个Turn一个存档一个省略，都完成（此前分别为Turn失败）。
- `tests/product_config/test_artifact_storage_full.py`：真实产品装配下命令已执行、存档存不下时结果为成功且带
  `artifact_omitted`，审计状态succeeded，Turn完成（此前`uncertain_effect`中断）；改动Review存不下时Route被拒绝、
  未交给Executor、文件不变、Turn完成（此前`uncertain_effect`中断）。
- `tests/models/test_tool_result_text.py`：截断的搜索与命令结果说明完整正文未保存。
