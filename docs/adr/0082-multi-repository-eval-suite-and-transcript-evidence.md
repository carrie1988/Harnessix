---
doc_type: adr
status: current
version: 2
code_revision: 92c62d428f51e9b40745f04f3bf0b820dbed1797
owners:
  - core
modules:
  - evals
  - session
  - models
related_adrs:
  - docs/adr/0044-coding-eval-contract-and-grader.md
  - docs/adr/0047-coding-eval-campaign-evidence.md
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
related_tests:
  - tests/evals/test_suite.py
  - tests/evals/test_task_pack.py
supersedes: []
---

# ADR-0082：采用计划先行的多仓库Eval Suite与脱敏Transcript证据

## 状态

接受，按0.9.2a～e分片实施。本文不表示多仓库任务集或真实Provider基线已经完成。

## 背景

现有Campaign可以对一个固定任务执行多次独立试验并汇总Token、Cost和延迟，但当前Catalog只有一个仓库中的一个
Bug Fix任务，无法回答Feature、Refactor、Test和Review质量，也没有统一人工干预率。完整Session包含评分所需事实，
同时也可能包含Prompt、代码、路径、Tool参数和输出，不能直接复制进低敏感度发布报告。

证据见[专项源码研究](../research/eval-suite-and-transcript-baseline.md)；当前实现见
[Evals模块设计](../modules/evals.md)。

## 决策驱动因素

1. 0.9.2必须覆盖五类真实软件工程任务和多个固定仓库；
2. 重复试验与跨任务聚合必须分层，避免重写已证明的Campaign；
3. 指标必须从Session、Eval Report和Cost Report重算，不能由CLI日志或人工表格填报；
4. Transcript证据必须可绑定Run/Turn，但不能泄漏正文；
5. 自动审批不能被误算为用户干预；
6. 任一Case缺失、身份漂移、成本未知或证据损坏必须失败关闭；
7. Task Pack和检查代码属于可信供应链，不能允许外部仓库任意命令直接在宿主执行。

## 候选方案

| 方案 | 满足项 | 缺失项 | 结论 |
|---|---|---|---|
| 扩展单个Campaign支持多任务 | 复用部分指标 | 状态、价格、任务身份和恢复语义混杂 | 拒绝 |
| 导出完整Session JSONL作为Suite报告 | 可追溯 | 高敏感、过大、难稳定版本化 | 拒绝 |
| 运行后从日志拼接CSV | 实现快 | 无计划、不可重算、崩溃后易部分成功 | 拒绝 |
| Campaign作为Case，Suite只聚合完整证据 | 分层清晰、可恢复、可重算 | 新增合同和任务集建设成本 | 采用 |

## 决策

Harnessix采用两层Eval结构：**Campaign负责同一任务的独立重复试验，Suite负责跨任务、跨仓库聚合。**

不可违反的不变量：

1. Suite Plan先于任何Case模型请求持久化，且绑定Case、任务版本、仓库Revision、Campaign Plan指纹和环境；
2. 合同至少覆盖`bug_fix/feature/refactor/test/review`和两个固定仓库；发布基线使用更严格的数据集阈值；
3. Suite Report只能在所有计划Case与Run具备完整、顺序一致的Campaign、Eval、Cost和Transcript证据后发布；
4. Transcript Evidence只保存完整Turn的SHA-256、身份和结构计数，不保存正文；
5. `harnessix-eval-runner`自动审批不计人工干预；其他Actor审批、Question、Steering和人工恢复均计入；
6. 任务成功率、测试通过率、人工干预率以分子、分母和可重算基点同时保存；
7. 已知成本必须同币种聚合，未知或部分成本不得填零；
8. 多仓库检查只通过版本化Task Pack和固定Container Profile执行，不执行模型提供或仓库动态发现的任意命令。

## 理由

Campaign已经具备Run隔离、成本完整性和崩溃恢复，作为Suite内层可以保持已验证语义。Suite新增的是任务分类、仓库
身份、Transcript摘要、人工干预与跨Case聚合，而不是第二套Agent Runner。Session继续保存高保真事实，Suite报告只保存
发布指标；这种分层同时满足可诊断性和最小披露原则。

## 后果

### 正面后果

- 可分别比较同任务稳定性和跨任务能力；
- 指标不依赖唯一Golden Patch或模型裁判；
- 报告可公开给发布流程而不携带代码正文；
- 自动审批与用户参与有明确区分；
- 新任务集不会把任意命令执行能力引入宿主。

### 负面后果与债务

- 需要建设Task Pack、固定镜像与至少三仓库十任务基线；
- Transcript摘要本身不能离线还原正文，深度诊断仍需受限Session数据库；
- 首版不使用模型裁判，Review任务必须设计确定性Oracle或人工双人复核合同；
- 原生Windows真实写任务仍受0.9.5平台发布边界约束。

## 兼容、安全与运维影响

现有`CodingEvalReport`和`CodingEvalCampaignReport`保持v1字节合同；Suite新增独立v1 Schema，不修改旧报告。
Suite Plan/Report采用私有`0600`、有界读取、拒绝符号链接、临时文件`fsync`和原子替换。原始Session、Artifact和
Provider正文不进入Suite Report。Windows私有ACL仍由运维目录策略保证。

## 验证方式

- Schema逐字节冻结与严格额外字段拒绝；
- 五类任务、至少两仓库、唯一任务/Campaign身份的正反合同测试；
- Campaign、Run、Turn、Token和Cost交叉绑定；
- 自动/人工审批、Question、Steering和人工恢复计数测试；
- 缺失Case、顺序漂移、摘要篡改、币种漂移、私有权限和符号链接测试；
- 后续真实多仓库固定镜像任务、崩溃恢复与受控Provider基线。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 源码研究 | [多仓库Eval与Transcript](../research/eval-suite-and-transcript-baseline.md) | 参考证据 |
| 重大变更设计 | [0.9.2详细设计](../changes/m09-2-eval-suite-and-transcript-baseline.md) | 实施边界 |
| 现行模块设计 | [Evals模块](../modules/evals.md) | 当前事实源 |
| Task Pack决策 | [ADR 0083](0083-built-in-immutable-coding-eval-task-pack.md) | 固定来源、物化和检查安全边界 |
| 测试 | [`test_suite.py`](../../tests/evals/test_suite.py)、[`test_task_pack.py`](../../tests/evals/test_task_pack.py) | Suite与Task Pack证明 |

## 被取代关系

无。ADR 0044、0047和0048继续分别拥有单次评分、同任务Campaign和真实Campaign执行语义；本文在其上增加Suite层。
