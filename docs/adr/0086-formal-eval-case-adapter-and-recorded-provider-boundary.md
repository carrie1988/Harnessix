---
doc_type: adr
status: current
version: 2
code_revision: a04606b829e6c4a32935b81c8ccc86ee5802d918
owners:
  - core
modules:
  - evals
  - agent
  - session
  - trusted_actions
related_adrs:
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
  - docs/adr/0085-versioned-third-party-eval-dataset-and-golden-boundary.md
related_tests:
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_grader.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# ADR-0086：以正式产品内核执行Task Pack Case并隔离Recorded Provider

## 状态

接受、已实现并完成验收。实现Revision `a04606b829e6c4a32935b81c8ccc86ee5802d918`已由
[CI 35479723645](https://github.com/carrie1988/Harnessix/actions/runs/35479723645)完成Linux Python 3.12/3.13、
macOS、Windows、固定Digest Container和Documentation六实例验收，0.9.2d2据此关闭。本决策不关闭
10 Case × 2 Trial完整离线Suite或真实Provider基线。

## 背景

0.9.2a～d1已经建立Suite、Campaign、不可变Task Pack、三仓十Case工程数据集和安全物化机制，但Suite的可信
Case端口还没有正式产品实现。若Case Adapter直接应用补丁、运行Docker命令或自行拼接评分结果，离线报告只能证明
Eval脚本可运行，不能证明Coding Agent的Session、审批、Trusted Action、Process Artifact和恢复链真实工作。

同时，离线基线需要一个不访问网络、结果确定的模型事件源。该事件源若读取Golden后直接把“正确结果”写入报告，
会混淆“产品链路验收”和“模型能力评测”，并破坏黄金答案隔离边界。

## 决策驱动因素

1. Task Pack Trial必须经过当前唯一Agent Runtime、Session和产品Trusted Action组合根；
2. 固定检查与Workspace Patch必须遵守正式Action合同、Policy、Approval、Audit、Executor和Reconcile；
3. Trial、Campaign和Suite必须复用既有报告及状态合同，不新增平行Schema；
4. 报告写入、状态提交或Campaign发布任一点崩溃后，不重复已完成模型请求和Action；
5. 自动审批范围必须可静态描述，超出Pack约束时失败关闭；
6. Recorded Provider只能作为可信测试输入，不得被表述为模型质量证据；
7. Golden不得进入Wheel、Adapter、Workspace、Session、Artifact或发布报告；
8. Review v1不得为本切片就地修改公共最终回答Schema。

## 候选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 扩展历史OpenAI Campaign执行器 | 复用部分顺序执行代码 | 绑定历史Harnessix任务、OpenAI配置和旧运行假设 | 拒绝 |
| B. 新建Eval Agent、审批器和Docker执行器 | 局部代码较短 | 形成第二套产品内核，无法证明正式链路 | 拒绝 |
| C. Case Adapter组合现有Agent与产品Action Runtime | 复用正式合同、审计和恢复事实 | 需要显式处理多层持久化边界 | 接受 |
| D. Golden直接生成报告 | 快速、确定 | 绕过Agent、工具和审批，证据无效 | 拒绝 |

## 决策

采用方案C，并固定以下规则：

1. `TaskPackCaseExecutor`实现现有`SuiteCaseExecutor`调用形状，只负责身份绑定、Trial顺序、Campaign前缀与报告发布；
2. `run_task_pack_coding_eval`为每个固定Run ID安全物化独立Workspace，创建或恢复一个Session和一个Agent Turn；
3. 产品组合根只装配当前Case的`run_profile.<profile_id>`、正式Workspace Patch、Git只读工具和Artifact；
4. 自动审批仅允许：
   - 工具名、`profile`和空`selectors`均精确匹配的固定Profile调用；
   - 文件数不超限、路径属于`allowed_changed_paths`且不含删除操作的正式`apply_patch_batch`；
5. Session请求身份固定为`coding-eval:<run_id>`。Run状态发布前以Session和Action账本为进行中权威事实；
6. 发布顺序固定为`Trial Report → Run State → Campaign Report → Campaign State`。重开先核对并吸收超前证据；
7. 成本从Turn模型尝试、固定价格快照和Billing Context重算；成本不完整时停止，不继续后续Trial；
8. Review v1把Oracle Finding ID作为最终回答`summary`中的独立词元严格校验，不修改已发布Schema；
9. Recorded Provider由调用方通过`TaskPackProviderFactory`注入。生产Adapter不导入、不定位、不读取Golden；
10. 离线测试可在测试夹具中读取Wheel外Golden，以构造Recorded Provider工具调用，但报告只能来自真实Agent执行事实。

## 理由

Case Adapter处于Suite与产品内核之间，最小职责是把固定Pack身份投影为正式产品执行，而不是重新实现产品能力。
复用Agent Runtime可得到真实Turn、预算、工具顺序和模型尝试事实；复用产品Action Runtime可得到审批、Action Audit、
Process Owner、Workspace Transaction和Artifact；复用Campaign/Suite合同可让成本、分类和脱敏聚合继续确定重算。

在公共最终回答v1中新增Review字段会扩大兼容范围。Finding ID已经是Pack内稳定、低敏感标识，把它作为`summary`
独立词元校验能在不改Schema的前提下形成确定性Review闭环；未来若需要结构化多Finding说明，应发布新的回答契约版本。

## 持久化与恢复后果

| 崩溃窗口 | 重开权威事实 | 恢复动作 | 禁止行为 |
|---|---|---|---|
| Plan后、首个Trial前 | Campaign Plan/State | 沿用固定Run IDs开始首个Trial | 生成新计划 |
| Thread或Turn创建后 | Session请求身份 | 恢复同一Turn | 新建第二Turn |
| Action执行中 | Trusted Action审计、Owner和效果账本 | 由现有Runtime恢复或Reconcile | 猜测成功、盲重放 |
| Trial Report后、Run State前 | 终态Session + 已发布Report | 重算并核对Report，只补Run State | 再打开Provider或重做Action |
| Run State后、Campaign前缀前 | 完整Run Report/State/Turn | 重算成本并推进一次连续前缀 | 跳过证据缺口 |
| Campaign Report后、State前 | 完整Trial前缀 + Report | 核对摘要，只补completed状态 | 重跑Trial |

完成Run可在没有Provider的情况下只读重开。未完成Turn仍由同一Agent Runtime和同一Action Owner恢复；Adapter不根据
目录存在、调用返回值或测试文本猜测效果结果。

## 安全、隐私与可观测性影响

- `_NoSecrets`拒绝任何Secret解析；固定Profile没有Secret引用且网络模式为`none`；
- Adapter再次核对Pack、Case、Task、Campaign、Profile和执行配置指纹；
- 自动审批Actor固定为`harnessix-eval-runner`，Suite把它统计为自动审批而非人工干预；
- Profile终端结果只投影状态、Return Code和内容摘要进入Eval观察，不复制完整输出到Suite报告；
- 公开报告不保存Prompt、回答、工具参数、工具输出、Diff、源码、绝对路径或环境变量；
- Recorded Provider的零费用只用于离线链路验收，不代表真实Provider价格或质量。

## 后果

### 正面后果

- 离线Case和真实产品使用同一Agent、Session、审批及副作用治理链；
- Trial/Campaign重开可由已有持久事实恢复，不依赖内存返回值；
- Suite Runner无需知道Provider、Golden、Container或产品工具细节；
- d3可直接组合十个Case，而不再引入执行旁路。

### 负面后果与债务

- 离线Recorded Provider是确定性脚本，不能证明模型泛化能力；
- Review v1的Finding ID位于`summary`，表达能力有限；
- 当前真实Container纵向测试依赖Linux Docker和固定镜像，本地无Docker时只能由CI验收；
- d3仍需覆盖完整20 Trial、Suite取消/超时/UNKNOWN/报告恢复和最终可发布基线。

## 验证方式

1. 单元测试证明计划先于执行持久化、边界取消不创建Provider、身份漂移在副作用前拒绝；
2. Grader测试证明产品Profile终端事实、Trusted Action Patch和Review Finding ID被严格投影；
3. 固定Container集成测试用两个独立Run执行失败检查、正式Patch、成功检查、Git核对和最终回答；
4. 集成测试分别在Trial Report和Campaign Report发布后注入宿主崩溃，重开不得重复已完成Provider或Action；
5. Ruff、Mypy、全量Pytest、文档门禁和六实例CI共同构成关闭证据。

## 关联资料

| 类型 | 路径/链接 | 关系 |
|---|---|---|
| 重大变更设计 | [0.9.2d详细设计](../changes/m09-2d-multi-repository-offline-baseline.md) | d1～d3总体设计与实现映射 |
| 现行模块设计 | [Evals模块](../modules/evals.md) | 当前行为事实源 |
| Trial实现 | [`task_pack_trial.py`](../../src/harnessix/evals/task_pack_trial.py) | Agent、Action、评分与Run恢复 |
| Case实现 | [`task_pack_execution.py`](../../src/harnessix/evals/task_pack_execution.py) | Campaign前缀、成本和Suite端口 |
| Container验收 | [`test_task_pack_execution.py`](../../tests/integration/test_task_pack_execution.py) | 两Trial正式纵向链与双崩溃窗口 |

## 被取代关系

无。未来引入新的Review回答结构或Recorded Provider协议时，应发布版本化合同和新ADR，不改写本决策的v1边界。
