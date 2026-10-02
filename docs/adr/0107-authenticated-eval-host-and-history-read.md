---
doc_type: adr
status: current
version: 1
code_revision: 730f0846641700c4c697d7cc6ba03cbf1a8364bc
owners: [core]
modules: [evals, session, secrets]
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0104-managed-session-key-and-default-root.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_authenticated_history.py
  - tests/evals/test_task_pack_publication.py
supersedes: []
---

# ADR-0107：评测复用产品认证宿主与同读完整历史

## 1. 背景与问题

Task Pack的执行与恢复必须遵守默认产品的Owner、独立Session Key及公开材料保护边界。
分别取得认证Thread快照和事件不能证明同一读版本；仅接受缓存投影也不能证明原事件正文未被替换。
评测基础设施不应保留无Binding的另一条“成功”路径，更不能通过补签旧Run或重置费用来恢复。

## 2. 架构决策与源码边界

1. 三条Trial路径共用[`TaskPackPublicationOwner`](../../src/harnessix/evals/task_pack_publication.py)，
   借用同一产品Root Owner、SessionPublicationBinding、Session/Artifact及Scope。
2. 真实Provider入口显式传入由原凭据Owner已经取得材料构造的保护Scope；不再次读取Keychain，
   不扫描环境，不使用Provider私有字段或Artifact Epoch代替来源。
3. 恢复只读原Key；已有状态缺Key或缺MAC拒绝，不能创建替代身份。仅真正全新空Run允许原产品初始化。
4. 恢复所需Thread/全事件从[`authenticated_thread_history`](../../src/harnessix/session/sqlite_history.py)
   的同事务原MAC和完整语义重放取得；复用原CancelToken及Owner检查。
   原Eval没有Run绝对单调期限，因此认证读取阶段明确新增120秒有限期限，在阶段入口一次捕获，
   全部底层读取共享，不按行/步骤刷新。原Session事件10秒上限及Turn执行预算保持；
   已过期的Turn仍可读取历史终态，但新只读阶段不得延长或恢复其执行资格。
5. `evals->secrets`是唯一新增一级包依赖，用于直接复用既有`SecretPublicationScope`和
   `EnvironmentSecretProvider`，不新增签发器、Key backend、编码器或认证算法。
   `secrets`不依赖`evals`，该边不形成新依赖环；原`evals->product_config/session/artifacts`继续保留。
6. 唯一Thread身份发现与完整历史读取属于认证宿主，而不是Trial执行编排。
   `TaskPackPublicationOwner.authenticated_single_thread`复用原控制与Reader，
   不增加新的Store、授权或缓存投影；Trial只消费已认证事实并决定原执行状态。

正式详设分别见[认证评测宿主](../changes/m09-r3-eval-publication-wiring.md)及
[同事务历史读取](../changes/m09-r4-authenticated-thread-history.md)。Reader返回普通事实，不是当前Root、批准或执行能力。

## 3. 备选方案与取舍

- **继续无认证评测Store**：实现简单，但与实际产品的恢复、公开保护语义分叉，不能接受。
- **在Eval内复制MAC/Key/完整Reader**：会形成第二权威、重复实现和迁移风险，拒绝。
- **通过不相关Facade隐藏Secret依赖**：并不消除依赖，只隐藏实际职责，降低源码可读性；不采用。
- **保留完整Snapshot而省略全事件认证**：缓存完整不代表原历史来源已证明，拒绝。

所选方案增加完整有界认证读取，保留原事件/正文/投影容量；新增120秒仅约束只读认证阶段，不改变执行或费用预算。
只批准精确一条新依赖边；600/100/20阈值、全部旧热点上限及原依赖环保持，不整表重建策略。

## 4. 失败、兼容与验证

旧未证明Run失败关闭，保留原数据及历史失败，不能续跑、补签或用于新质量成绩。
Parent取消、期限、Owner关闭、错Key和原正文篡改均须在恢复动作与Provider前拒绝。
Owner的OS/SQLite异常仅由Reader私有载体隔离；真实回滚/关闭失败和父取消保持原资源结算优先级。
真实Provider认证材料必须由同一显式Scope贯穿，低层合成/录制夹具的空Scope不能进入真实Suite。

取消兼容变化明确限定为已取消且含已完成历史的非空前缀：此时必须传播原`TurnCancelled`，
不能绕过完整认证返回缓存的停止结论。无历史读取的空前缀仍返回原停止原因；
未取消的报告、停止原因及费用判断顺序不变。拒绝时不调用Provider、不重放Trial、不改Campaign前缀。

验证必须包含真实SQLite、完整事件替换、同读版本、恢复前无Provider调用、原资源结算和精确源码身份。
离线装配通过不代表真实编码质量；不改Task Pack、Grader、模型参数、70元账本或未决费用规则。
R3完整20 Trial、Windows消费者、Beta与同候选R1～R6发布门禁继续开放。
