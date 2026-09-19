---
doc_type: adr
status: current
version: 2
code_revision: ffd3db4e83a807ab3029c479fb4650216240b4f7
owners:
  - core
modules:
  - evals
related_adrs:
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
related_tests:
  - tests/evals/test_suite_execution.py
  - tests/evals/test_campaign_execution.py
supersedes: []
---

# ADR-0084：采用顺序单写者、证据前缀恢复的Eval Suite Runner

## 状态

接受且已实施。实现Revision `ffd3db4e83a807ab3029c479fb4650216240b4f7`已由[CI 35465458256](https://github.com/carrie1988/Harnessix/actions/runs/35465458256)完成六实例验收，路线图0.9.2c据此关闭；0.9.2d多仓库离线基线和
0.9.2e受控真实Provider基线不在本文完成范围内。

## 背景

0.9.2a已经建立Suite Plan、Case Report和Suite Report，0.9.2b已经建立不可变Task Pack与固定检查Profile，
但两者之间缺少生产级调度语义。若调用方只用`for`循环依次运行Campaign，进程在Case完成、状态推进或最终报告
发布附近退出时，无法判断应该重放Case、只补状态，还是停止。错误重放会重复Provider请求、审批或外部效果；
错误跳过则会把缺失证据计入基线。

Suite层不应复制Agent Runtime、Provider、审批或工具执行逻辑。单个Case仍由Campaign及其下层Run负责同Run ID恢复，
Suite只拥有跨Case顺序、完成前缀、总费用停止线和最终聚合发布。

## 决策驱动因素

1. Suite Plan必须先于任一Case执行适配器持久化；
2. 同一Suite根目录同一时刻只允许一个写宿主；
3. 完成进度只能是计划Case顺序的连续前缀；
4. Case证据已落盘但Suite状态未推进时，只能消费证据，不能再次执行Case；
5. 停止状态必须显式`resume=True`，进程崩溃形成的`running`状态则可自动重入同一Case；
6. 成本未知或总费用达到停止线时，不得调度下一个收费Case；
7. 最终报告发布确认丢失时，只核验并补交完成状态；
8. 状态与公开结果不得保存Prompt、模型正文、Tool参数/输出、Diff、绝对路径、Secret或Provider原始错误；
9. 0.9.2c不能虚构尚未完成的Task Pack到Campaign适配链和真实Provider数据。

## 候选方案

| 方案 | 优点 | 关键缺陷 | 结论 |
|---|---|---|---|
| 调用方临时`for`循环 | 实现最少 | 无持久状态、锁、恢复和费用边界 | 拒绝 |
| Suite直接内嵌Agent/Provider实现 | 表面一体化 | 复制Campaign职责，形成第二执行主链 | 拒绝 |
| 并行执行全部Case | 总耗时低 | 费用停止滞后，恢复和资源竞争复杂 | v1拒绝 |
| SQLite分布式调度 | 查询与扩展强 | 本地基线阶段过早引入服务状态和租约 | 暂缓 |
| 文件单写者、顺序执行、Case证据前缀 | 边界小、可审计、与Campaign恢复组合 | 只支持单主机且吞吐有限 | 采用 |

## 决策

Harnessix采用以下不变量：

1. `CodingEvalSuiteRunConfig`同时冻结Suite Plan、有序Campaign Plans、私有Work Root和单币种总费用停止线；
   全部Campaign Run ID必须跨Case唯一，配置Fingerprint参与恢复身份；
2. `suite-plan.json`在Case执行适配器首次调用前以0600、临时文件、`fsync`和原子替换发布；既有Plan只能逐字段相等；
3. `.suite.lock`是0600普通文件，使用非阻塞独占文件锁；目录必须为0700且不能是符号链接；
4. `suite-state.json`只保存连续`completed_case_ids`、当前Case、已知费用、稳定停止原因和最终报告摘要；
5. 每个Case使用由计划序号派生的固定目录`cases/case-NNN`，不把外部Case ID拼接进路径；完整脱敏Case Report先于
   完成前缀提交；
6. 重开扫描全部Case槽位，拒绝缺口后的孤立报告。若持久Case Report超前于状态，只核验Campaign Plan和摘要后推进一次；
7. Case执行适配器必须使用传入的同一Case/Campaign和固定目录恢复下层运行。Runner自身不重试适配器、不创建Provider、
   不审批，也不执行工具；
8. `cancelled`、`fee_limit_reached`、`cost_unknown`、`evidence_missing`和`runtime_failed`进入`stopped`；默认重开只返回
   白名单状态，只有显式`resume=True`才允许重入当前Case；
9. 未捕获进程故障保留`running + current_case_id`，重开可调用一次相同Case适配器，由Campaign/Run权威事实完成对账；
10. 全部Case且成本完整后才发布`suite-report.json`。报告已存在时先从Case证据重建并逐字段核对，再补`completed`状态；
11. 总费用停止线只在Case边界判定，不冒充Provider硬额度；当前Case已经发生的费用不能回滚；
12. 状态、Case结果和Run结果均采用严格v1 Schema，额外字段、错误类型、身份漂移和证据损坏一律失败关闭。

## 理由

“Case Report先于Suite State”把最危险的崩溃窗口转化为可观察事实：报告存在且身份、Campaign计划和摘要均有效时，
推进前缀不产生外部效果；报告不存在时，Runner只重入同一Case及同一Campaign Plan，下层负责沿用固定Run ID恢复。
因此Suite不需要知道Provider请求、审批或Process Lease细节，也不会形成第二套执行账本。

停止和崩溃采用不同语义。停止是已经形成的业务决定，必须由操作者显式恢复；崩溃没有形成停止决定，允许自动重入
当前Case以读取权威事实。两者若统一自动恢复，会把取消或费用停止错误地变成继续收费。

## 后果

### 正面后果

- Case边界崩溃不会重跑已持久化Case；
- 最终报告确认丢失不会重新执行Campaign或重新聚合不可信正文；
- Suite与Campaign职责清晰，保持单一Coding Agent执行主链；
- 总费用和成本完整性在新Case创建前形成稳定停止点；
- 文件布局、状态、报告和测试可离线审计，不依赖新数据库或独立服务。

### 负面后果与债务

- v1只保证单机文件锁，不提供跨主机Lease、Fencing或分布式调度；
- Case执行适配器到Task Pack/Campaign的正式产品装配留给0.9.2d；
- 取消只在协作检查点生效，同步文件系统操作不能被中途撤销；
- Case内部停止时，Suite只能记录稳定原因，内部Trial费用和恢复细节仍由Campaign状态持有；
- Windows当前只承诺合同和读取路径，POSIX权限/no-follow真实执行边界继续由0.9.5处理。

## 兼容、安全与运维影响

现有Run、Campaign、Suite Plan和Suite Report v1均不改版本。新增四个v1公共合同、Case Report/State原子I/O、
Suite Runner和共用Eval执行文件边界；Campaign只把原有私有目录与锁实现改为共用函数，状态和调用接口不变。

运维恢复必须提供与原始配置完全相同的Plan、Campaign Plans、Work Root和费用停止线。修改停止线、Case顺序、Run ID、
任务、环境或价格会改变配置Fingerprint并失败关闭；需要改变范围时创建新Suite版本和新Work Root。

## 验证方式

- 首个Case前Plan与Ready/Running State持久化；
- 固定顺序完整执行及完成后零Case重放；
- Case Report写入后崩溃，重开跳过该Case；
- Suite Report写入后崩溃，重开只补完成状态；
- 协作取消和显式恢复；
- Case返回证据缺失后默认不重入，显式恢复后继续；
- 成本未知和聚合费用停止线阻断下一个Case；
- 错Case、缺口证据、Campaign/Run身份漂移和锁冲突失败关闭；
- 四份公共Schema冻结、Ruff、Mypy、Evals回归、全仓回归与文档门禁。

实现Revision `ffd3db4e83a807ab3029c479fb4650216240b4f7`本地完成3514项通过/20项跳过、32个变化文档Mermaid真实渲染和Wheel导入冒烟；
[CI 35465458256](https://github.com/carrie1988/Harnessix/actions/runs/35465458256)进一步通过Linux Python 3.12/3.13、macOS、Windows、固定镜像Container和Documentation六实例。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 详细设计 | [0.9.2c可恢复Suite Runner](../changes/m09-2c-recoverable-suite-runner.md) | 完整流程、字段、失败矩阵和伪代码 |
| 总体设计 | [0.9.2 Eval Suite与Transcript](../changes/m09-2-eval-suite-and-transcript-baseline.md) | 上层里程碑边界 |
| 模块设计 | [Evals模块](../modules/evals.md) | 当前实现事实源 |
| 执行合同 | [`suite_execution_contracts.py`](../../src/harnessix/evals/suite_execution_contracts.py) | Config、Case Result、State、Run Report |
| Runner | [`suite_execution.py`](../../src/harnessix/evals/suite_execution.py) | 锁、前缀、停止、恢复和发布 |
| 原子I/O | [`report.py`](../../src/harnessix/evals/report.py) | Plan、Case Report、State与Suite Report |
| 测试 | [`test_suite_execution.py`](../../tests/evals/test_suite_execution.py) | 正常、失败、恢复与Schema |

## 被取代关系

无。本文细化ADR 0082中规划的Suite Runner，不改变ADR 0048的Campaign职责或ADR 0083的Task Pack边界。
