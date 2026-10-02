---
doc_type: validation-evidence
status: draft
version: 2
code_revision: pending
owners: [evals, security]
modules: [evals, product_config, session, artifacts, secrets]
related_adrs:
  - docs/adr/0095-versioned-secret-publication-scope.md
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0104-managed-session-key-and-default-root.md
  - docs/adr/0107-authenticated-eval-host-and-history-read.md
related_tests:
  - tests/evals/test_task_pack_publication.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
supersedes: []
---

# R3 认证宿主与完整历史消费：集成验证报告

## 1. 结论、范围与精确身份

**离线接线通过：133 PASS／0 XFAIL／0 SKIP；八个指定输入 Ruff 通过，四个 Eval 源 Mypy 通过。** 原事件正文 MAC strict xfail 已移除并真实通过，失败类型明确断言为 `publication_history_unproven`。

研究输入为 `80c1dad13c98aaabb2db1630c62134a241df44aa`，实际集成基线为 `730f0846641700c4c697d7cc6ba03cbf1a8364bc`。后者仅增加不相交路径，不代表未提交候选已进入该提交。源码身份以 [owned-source-sha256.json](owned-source-sha256.json) 为准；文档保持 draft/pending。

实现只消费已有正式 `SQLiteSessionStore.authenticated_thread_history`。Session／Error／Artifact／Key backend 源、费用规则、Pack、Profile、模型参数、Grader、Schema 和实际旧 Run 不修改。十个保护输入逐字节未变；原 48 成员封存包及 Manifest 重算通过，见 [protected-inputs-check.json](protected-inputs-check.json)。

这是认证宿主离线整改结果，不是 R3 新成绩、商用验收、Windows 原生验收、真实 Provider 复验、安装或费用状态解除。

## 2. 实际调用链与读取控制

详设：[认证宿主接线 SDD](../../changes/m09-r3-eval-publication-wiring.md)，正式同读端口：[Session 详设](../../changes/m09-r4-authenticated-thread-history.md)，模块依赖：[ADR0107](../../adr/0107-authenticated-eval-host-and-history-read.md)。

- 单 Run 的完成、终态和执行三分支借用同一 Owner／Binding／Store／Scope；前缀独立重开各 Run Owner，只加载原 Key，不能因缺材料初始化或 fallback。
- 非空原历史在 Provider Factory、Action Runtime 和 AgentRuntime 主动恢复之前完整验真。完成重读、终态判断、驱动恢复都消费同读 carrier 的 Thread，不拼 `get_thread/events`，不只验末事件。
- 默认内部认证读取阶段 120.0 秒，入口一次计算绝对 monotonic deadline；原 CancelToken 与当前 Run Owner 回调贯穿。Case 全部完成前缀共用一个控制；主 Trial 完成／终态／执行前门禁共用一个控制。
- 原身份发现用同一 deadline 的剩余时间托管 `cancel.run(thread_ids())`，取消／期限／父 Task 退出均结算，不刷新 TTL。已有库首次发现也显式转发控制。已有库不初始化／迁移／切换 WAL；新库才初始化。
- Runtime 入口可能合法改变开放 Turn，因此驱动开启真正新的只读恢复步骤，取得最新完整 carrier；执行后的完成重验也属于新只读步骤。两者不是每行或底层 read 的 TTL 刷新。
- 120 秒不构成新的 Run 执行预算。终态历史可在原 Turn 预算过期后只读验真，原 `remaining_seconds(turn)`、Case Budget、Operation 和费用控制不变。Session 原单次事件 Reader 10 秒上限不改；产品原 Key 加载 5 秒语义独立保留。
- `eval.provider/1` 只是应用凭据引用版本；默认 Factory 核对实际材料版本，不支持版本在 Provider 构造前沿原错误拒绝。历史 Scope 摘要不能反推不存在的旧映射，认证元数据不授执行权。

## 3. 明确的兼容／取消语义变化

**这不是纯夹具修正，也不是旧全部语义不变。** 旧 durable-stop 路径允许已取消的非空完成前缀直接返回缓存停止结论；新增完整认证要求沿原 Token 读取，因此该组合现在传播 `TurnCancelled`。不以 freshToken 或忽略取消来获得 PASS。

`test_case_evidence_stop_is_durable_without_trial_replay[0/1]` 明确验证：

| 情况 | 当前结果与必须保持 |
|---|---|
| 未取消，非空已完成前缀 | 原 `evidence_missing` 停止及费用金额、完整 prefix 保持；即使 prefix 内存在费用未知，停止原因仍优先 |
| 已取消，非空已完成前缀 | 传播 `TurnCancelled`；控制 `.cancel is 原 token`；0 Provider、0 Trial 重放、0 Campaign 状态写入；报告／状态及静止 prefix 全部文件 SHA 保持 |
| 空前缀，取消或未取消 | 原 stop 返回不改变；同样 0 Provider、0 Trial 重放、0 状态写入 |

前缀 SHA 使用新合成夹具，在构造阶段结算 WAL、显式 DELETE 日志模式后捕获；这证明静止主库只读路径，不宣称活跃 WAL／SHM 不可变，也不改变产品配置或实际 Run。

首轮旧断言的 FAIL 保留在 [pytest-v1.txt](pytest-v1.txt)：114 PASS／1 FAIL。它是上述兼容性变化的实际暴露，不能删除或改称夹具错误。

## 4. 真正运行的验证矩阵

| 覆盖 | 实际验证 |
|---|---|
| 原 MAC 缺口 | 原 strict xfail 真 PASS；篡改首事件／中间事件、保留投影与 Seal，完成／终态／开放 Turn 都拒绝 |
| 请求与恢复前置 | 错误发生前 Provider、Action、AgentRuntime 调用计数 0，静止旧合成文件 SHA 不变 |
| 同读消费 | 拦截实际 SQLite carrier 端口核对原 cancel／deadline／Owner 回调，禁止缓存 `get_thread`；所有完整事件被消费 |
| 期限 | 同阶段重复发现不刷新 deadline；多 Run prefix 控制对象完全相同；过期终态执行预算不阻塞只读读取 |
| 生命周期 | 原取消、绝对期限、Binding close、Owner close、Owner 原 OS 错误在真实 carrier 读取中拒绝；发现读取消／期限／父 Task 退出均结算 |
| Runtime 后恢复 | 驱动重新消费完整同读历史；入口前快照取得后再篡改中间原事件，驱动拒绝，不复用旧投影 |
| Scope | 非空同源快照、环境漂移冻结、实际材料版本错误拒绝；预算脚本仅既有 Scope 转发，文件 SHA 未变 |
| 非回归 | 原五个 selector 的审批、成本未知、证据缺失、报告窗口、CLI 与完成前缀均实际回归 |

最后完整日志为 [pytest-final.txt](pytest-final.txt)：**133 passed in 3.43s**。原接线前日志 [pytest-before.txt](pytest-before.txt) 是 114 PASS／1 strict XFAIL。中间 v2～v4 的失败日志保留：实际 Factory 契约字段错用已改为材料自身版本核对；新夹具的用户输入、Root 地址、私有目录和 Campaign 状态构造问题已按正式合同纠正；SQLite 字节回归同时推动已有库不初始化，并明确静止日志模式的证明范围。v5～v8 及最终各轮均通过；最终结果以冻结源 SHA 为准。

[Ruff](ruff-final.txt) 和 [Mypy](mypy-final.txt) 均通过。四图已用本地 mmdc／Chrome 渲染为 SVG 与 PNG，并逐图查看；第二图的控制捕获位置已与源码对齐，见 [diagram-verification.json](diagram-verification.json)。

## 5. 精确复验 selector 与命令

[selectors.json](selectors.json) 给出五个完整文件 selector，期望 133 PASS／0 XFAIL／0 SKIP。解释器必须为集成树的 `.venv/bin/python`，源码路径只为该树 `src`；不得把 repo 根加入 `PYTHONPATH`。`run_offline_tests.py` 也移除 repo 根、显式加载测试包并打印来源。

在集成树执行（`TREE` 必须为实际该集成树的绝对路径，`TMP` 为新的自有隔离目录）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$TREE/src"   "$TREE/.venv/bin/python" -B   "$TREE/docs/validation/r3-authenticated-host-integration-2026-10-02-v1/run_offline_tests.py"   "$TMP"   tests/evals/test_task_pack_publication.py   tests/evals/test_task_pack_execution.py   tests/evals/test_task_pack_evidence_stop.py   tests/evals/test_provider_suite_execution.py   tests/evals/test_provider_suite_cli.py
```

重点取消语义 selector：

```text
tests/evals/test_task_pack_evidence_stop.py::test_case_evidence_stop_is_durable_without_trial_replay
```

原缺口 selector：

```text
tests/evals/test_task_pack_publication.py::test_completed_recovery_requires_original_event_body_mac
```

先核对冻结八个源 SHA，再执行 selector。独立复验应写入新的自有证据目录，不能向封存包追加夹具或覆写日志；未执行实际模型／费用入口。

## 6. 最终八个源／测试 SHA256

| 路径 | SHA256 |
|---|---|
| `scripts/run_engineering_provider_suite_budgeted.py` | `9149f797c2539cc2bbb4f12d70813e3a85c0bcc1c509bdbbef39d7e046222a11` |
| `src/harnessix/evals/task_pack_publication.py` | `81f6df4ffbee6b9b9a30c2f73307428309b041eb88e191568b1f57e4fdee6bbb` |
| `src/harnessix/evals/task_pack_trial.py` | `7851ed245fd82fec0b06cbce0e0b437abc8fa1300015a660686c6e217c69988f` |
| `src/harnessix/evals/task_pack_execution.py` | `1609740aab6c89dd70d000943bc806ab1622333c005c6d6c2b2c2a7098d3d774` |
| `src/harnessix/evals/provider_suite_execution.py` | `f5925a8223c376f8e63aa6ef2556fe34fd30575040a24eb1e7332206ef7970fa` |
| `tests/evals/test_task_pack_publication.py` | `4abc8217fcbc7ee0cda956f0914a0a6ff0e548528e9fe8c57792846623bc761f` |
| `tests/evals/test_task_pack_evidence_stop.py` | `618af9f5af30c1f816caeccfab3930d8a494446385ec595d730d791d68eaf65f` |
| `tests/evals/test_provider_suite_execution.py` | `73b56f26296a2303df0771b85903d58b5eb9de2738ca4dd9e5675770489a7846` |

详设 SHA 单独记录在 [documentation-source-sha256.json](documentation-source-sha256.json)。[integration-owned.diff](integration-owned.diff) 只比较原冻结自有九输入与当前候选，不含其他模块；原封存包历史 SHA 不改写。较早的阶段性冻结 SHA 保留为 `owned-source-sha256-v1.json`，最终只认 `owned-source-sha256.json`。

## 7. 清理、交付与剩余验收

仅清理本新包创建的测试夹具与 Mypy 缓存，不触碰其他归档。交付记录、图和脚本不包含实际 Run 正文、真实 Key／凭据或 SQLite 夹具；目录 0700、记录 0600，夹具测试进程 umask 022。Manifest 列出全部成员及其字节 SHA，由独立只读验证脚本再次核对。

独立源码复审签字、精确候选唯一 Wheel 安装、全仓文档变更关系门禁和发布收口由集成验收另行进行。本包只验证详设／报告的元数据、相对链接、章节和 Mermaid 结构，不冒充全仓门禁。没有 stage／commit／push、CI、网络、容器、模型请求、实际 Keychain 或账本操作；不影响独立 Windows 诊断分支。


## 8. 原封存包集成副本的权限观察

原 48 个成员与 Manifest 的 SHA 重算均通过，但集成副本根目录实际为 `0755`，不满足原包 `0700` 约束；原完整 verifier 因根目录权限断言 FAIL。另有 48 个成员不满足 `0600`。独立原冻结工作树根目录仍为 `0700`。见 [权限观察](original-package-permission-observation.json)。

这是副本归档权限问题，不是原成员字节漂移、Session 认证失败或测试通过的反例。本切片没有修权限或改原封存包；归档负责方应恢复原权限后独立重验。不能将仅成员 SHA 通过称为原包完整 verifier PASS。新集成包自身目录和成员权限另按 Manifest 验证。
