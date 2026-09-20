---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners:
  - core
modules:
  - evals
  - agent
  - trusted_actions
related_adrs:
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
  - docs/adr/0085-versioned-third-party-eval-dataset-and-golden-boundary.md
  - docs/adr/0086-formal-eval-case-adapter-and-recorded-provider-boundary.md
related_tests:
  - tests/evals/test_suite_execution.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# ADR-0087：确定性组合完整离线Eval Suite并隔离证据生成

## 状态

提议，进入0.9.2d3实施。本文只冻结完整离线Suite的组合、恢复和证据发布边界；在20 Trial真实固定Container
场景及全矩阵CI通过前，不得据此宣称0.9.2d完成。

## 背景

0.9.2a～d2已经形成Suite/Campaign合同、顺序单写者Suite Runner、三仓十Case不可变Task Pack，以及复用
正式Agent与产品Trusted Action的Case Adapter。当前仍缺少把十个Case、每Case两个Trial一次性组合为固定运行计划的
正式入口，也没有可发布的20 Trial脱敏报告。

若直接在测试中手写十组随机Campaign，重开时无法稳定重建同一配置；若为d3再实现一个Runner或让Golden直接生成
报告，则会绕过已经验收的持久计划、连续证据前缀、成本停止线和产品执行链。若把Recorded Provider放进生产包，
还会模糊确定性链路验收与真实模型质量评测的边界。

## 决策驱动因素

1. d3必须复用现有`run_coding_eval_suite`和`TaskPackCaseExecutor`，不能形成第二条执行链；
2. 首个Provider事件前必须固定Suite、Campaign和20个Run身份；
3. 同一`Suite ID + Task Pack版本`在崩溃重开时必须得到逐字段相同的执行配置；
4. Golden只能用于测试侧构造Provider事件，不能进入Wheel、产品Adapter、Session或报告；
5. 完整报告必须能由Case/Campaign/Transcript/Test证据重算，且不复制用户内容、源码、Diff或宿主路径；
6. 取消、超时、崩溃、`UNKNOWN`、完成前缀和报告发布恢复都必须有权威层测试；
7. d3不修改已发布Suite、Campaign、Task Pack或最终回答Schema。

## 候选方案

| 方案 | 满足的驱动因素 | 不满足的驱动因素 | 成本/风险 |
|---|---|---|---|
| A. 测试内随机构造十组Campaign | 可快速运行全部Case | 配置不可稳定重建，没有正式组合入口 | 崩溃恢复依赖内存夹具 |
| B. 新建完整Task Pack Runner及报告Schema | 可定制流程 | 复制现有Suite/Campaign状态机和合同 | 双链漂移、恢复语义分叉 |
| C. 纯组合器生成现有Run Config，继续调用现有Runner | 身份稳定、复用全部正式合同 | 需要测试侧证据生成器与CI编排 | 最小增量，可由现有门禁验证 |
| D. Golden直接投影20 Trial报告 | 最快得到通过数字 | 绕过Agent、Action、Artifact和Grader | 报告没有产品证据价值 |

## 决策

选择方案C，并固定以下不变量：

1. 新增的生产代码只负责从已重新核验的内置Task Pack构造`CodingEvalSuiteRunConfig`；
2. Case顺序严格沿用Pack Manifest顺序，每个Campaign严格包含两个Run；
3. Campaign ID和Run ID使用调用方提供的Suite UUID作为命名空间，通过UUIDv5及Case ID、Trial序号确定性派生；
4. 全部Campaign共享同一`CodingEvalEnvironment`、零费用Recorded价格快照、Billing Context和创建时间；
5. 组合器不接受Provider、Golden路径、任意命令、外部URL或Secret，也不读取Golden；
6. 执行仍由`run_coding_eval_suite → TaskPackCaseExecutor → run_task_pack_coding_eval`完成；
7. Recorded Provider与Golden解析只放在测试/CI支持代码中，不进入`src/harnessix`或Wheel；
8. 发布物只包含严格解析后的Suite Plan、Suite Report和低敏摘要；私有运行目录、Session、Artifact、工具正文、
   Patch和Golden不发布；
9. 完整场景在首个Case报告发布后和最终Suite报告发布后分别注入一次宿主崩溃，重开沿连续证据前缀恢复，
   20个Run各自最多打开一次Recorded Provider；
10. 取消与停止由Suite Runner验证，Turn超时由Agent Runtime验证，效果`UNKNOWN → reconcile`由Trusted Action与
    Product Action验证；d3不在聚合层伪造或吞并这些权威失败。

## 理由

Suite Runner已经拥有计划先行、单写者锁、连续Case证据前缀、显式停止恢复、费用停止线和最终报告恢复；Case Adapter
已经拥有Campaign计划、Trial前缀、Agent/Action事实和双报告窗口恢复。d3真正缺少的是确定性组合和完整规模证据，
而不是新的状态机。纯组合器能让配置在进程退出后由稳定输入重建，并让既有Runner继续拥有唯一执行语义。

UUIDv5派生身份比把随机UUID写入新的配置文件更小：现有Suite Plan与每Case Campaign Plan已经是持久权威事实，
无需新增Schema或迁移。Recorded Provider保留在测试侧，则可以证明产品纵向链路，又不会把Golden驱动的确定性结果
误写为模型泛化能力；真实Provider质量由0.9.2e单独验收。

## 后果

### 正面后果

- 完整离线Suite只有一条Runner、一个Case Adapter和一套报告合同；
- 同一Suite身份可稳定重建20个Run，计划漂移会在副作用前失败；
- 20 Trial可验证真实Agent、审批、Container检查、Workspace Patch、Artifact和Grader主链；
- 公开证据体积小、可重算，并与私有Session/Workspace隔离；
- d3完成后可直接复用同一Suite范围进入0.9.2e真实Provider基线。

### 负面后果与债务

- Recorded Provider按Golden产生确定性工具调用，只能证明链路正确，不能证明模型能力；
- 完整固定Container场景耗时高于普通单元测试，只在Linux Container CI执行；
- macOS和Windows只验证组合合同及非Container层，三平台发行门槛仍由0.9.6关闭；
- 未来Task Pack每Case Trial数变化时必须发布新的组合版本或显式参数合同，不能静默改变v1基线。

## 兼容、安全与运维影响

- 不修改公共JSON Schema、数据库迁移、Agent Protocol或产品CLI；
- 不恢复已经删除的Action HTTP/Worker服务，也不增加Eval Worker；
- 新组合器只接受已经核验的内置Pack和可信宿主字段；运行目录继续使用0700目录、0600状态文件和单写者锁；
- 固定检查继续使用Digest镜像、`network=none`、非root、只读Workspace和资源上限；
- CI证据发布前必须重新严格解析报告，并拒绝绝对路径、Golden内容、Prompt、工具参数/输出、Diff及Secret标记；
- 回滚只删除新组合入口和CI步骤，现有Suite/Case/Campaign报告仍可按原合同读取。

## 验证方式

1. 单元测试证明十Case、五类任务、三仓和20个Run精确组合，UUID派生确定且Suite隔离；
2. 组合器拒绝伪造Pack、相对运行目录、非法Revision和非严格时间字段；
3. Linux固定Container执行10 Case × 2 Trial，全部报告由正式Agent与产品Action链产生；
4. 首Case证据与最终报告两个崩溃窗口恢复后，已完成Provider和Action不重放；
5. 既有Suite取消、Agent超时、Trusted Action `UNKNOWN → reconcile`专项回归共同构成失败语义证据；
6. 证据发布检查证明公开JSON可严格重读、汇总可重算且不包含禁止字段或宿主路径；
7. Ruff、Mypy、Schema、可读性、文档、全量Pytest和六实例CI全部通过后，才接受本文并关闭0.9.2d3。

## 关联资料

| 类型 | 路径/链接 | 关系 |
|---|---|---|
| 源码研究 | [Eval Suite与Transcript基线研究](../research/eval-suite-and-transcript-baseline.md) | Suite证据和报告边界 |
| 重大变更设计 | [0.9.2d多仓库离线基线](../changes/m09-2d-multi-repository-offline-baseline.md) | d1～d3实施与失败矩阵 |
| 现行模块设计 | [Evals模块](../modules/evals.md) | 当前合同、Runner、Task Pack和Case Adapter事实源 |
| 既有测试 | [`test_suite_execution.py`](../../tests/evals/test_suite_execution.py)、[`test_task_pack_execution.py`](../../tests/integration/test_task_pack_execution.py) | Suite恢复与正式产品纵向链 |

## 被取代关系

无。本文扩展ADR 0084～0086的组合边界，不取代其Runner、Task Pack或Case Adapter决策。
