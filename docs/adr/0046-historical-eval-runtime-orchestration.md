# ADR 0046：历史任务的Agent Runtime、审批、Worker与评分编排

- 状态：已接受并实现0.5.5b2
- 日期：2026-09-06
- 范围：受管执行副本、运行状态、固定审批、外部Worker、恢复与报告发布

## 1. 背景与目标

0.5.5b1已经把首个Harnessix真实历史缺陷固定为可验证来源，能够构造不含后续修复历史的单提交私有仓库，并由宿主执行隐藏行为检查。此前仍缺少从任务Prompt到Session、模型工具调用、Process/Patch审批、外部Worker、Git反馈、最终回答和确定性评分的完整运行编排。

本片目标是让同一个历史任务经过现有正式生产路径完成端到端运行，不新增Eval专用写入或进程旁路。验收使用确定性脚本Provider证明基础设施组合、持久恢复和评分事实；真实Provider的多次成功率、成本与时延基线仍属于0.5.5c。

## 2. 架构决策

### 2.1 两层私有工作区

```text
固定来源revision
      │ git archive + 身份核对
      ▼
run/workspace                 历史物化层
      │ baseline hidden check
      │ 全部tracked普通内容快照
      ▼
run/managed/<workspace-id>/workspace
      │
      ├─ CodingToolRuntime：只读、Git状态与差异
      ├─ ManagedPatchBridge：允许文件、计划、审批、执行、核对
      └─ RunTestsAgentBridge → Action Plane → ActionWorker → 固定检查
```

历史物化层负责证明来源revision、tree和缺陷基线，不接受Agent写入。执行层由既有`PatchWorkspaces`创建并登记，`ManagedPatchBridge`只允许修改清单内文件。这样不需要让Patch桥接接管一个没有副本账本的任意目录，也不需要增加Eval专用文件写函数。

当前历史树有一个工具作用域故意隐藏的`.env.example`。为保持执行仓库与固定Git树完全一致，受信编排器在初始副本构造期间复制该普通文件和私有`.git`元数据；它不进入Managed Patch清单，Agent只读工具拒绝隐藏路径，Patch审批也不能修改它。创建后必须由固定Git端口证明HEAD等于物化基线且状态、暂存区和未跟踪集合均为空。

### 2.2 不新增Agent或Action实现

`run_historical_coding_eval`组合现有组件：

- `AgentRuntime`负责Turn、模型调用、工具历史、预算和Session状态；
- `CodingToolRuntime`负责受作用域约束的读取及固定`git_status`/`git_diff`；
- `ManagedPatchBridge`负责唯一允许文件的精确Patch及独立审批；
- `RunTestsAgentBridge`把模型选择的`focused`解析为宿主固定检查argv；
- `ActionService(auto_execute=False)`和`SQLiteEffectJournal`保存唯一Process审批与效果事实；
- `ActionWorker`独立领取READY Action并执行，编排器只观察结果；
- 0.5.5a评分器从持久Turn、隐藏检查和Git证据生成报告。

Provider实例及其网络生命周期由调用方持有。编排器不读取API Key、不隐式打开Provider，也不自动重试模型调用。

## 3. 运行流程

新运行按以下顺序执行：

1. 物化并核对固定历史来源；
2. 在物化层运行基线隐藏检查，要求行为检查唯一存在且失败；
3. 要求物化Git工作区完全干净，随后构造受管执行副本并复制相同Git基线；
4. 发布`run-state.json`的`ready`状态；
5. 创建或恢复唯一Session Thread，发布`running`状态；
6. 使用任务Prompt和预算创建幂等Turn；
7. 只批准任务声明的`focused`测试Profile或允许路径的单文件Patch；
8. Process审批后由独立Worker执行原Action，非零退出形成`passed=false`反馈而不是运行器失败；
9. Turn终结后在执行副本运行行为与回归隐藏检查，采集HEAD、状态、Diff摘要和路径分类；
10. 使用0.5.5a评分器生成`report.json`，再将运行状态发布为`completed`。

副本构造最多导入256个普通文件、32 MiB正文，并为每个基线镜像执行`FULL`同步的SQLite持久化，
属于受信编排器的有界批量物化，不是模型可调用的一次读取。模型工具的默认单操作预算保持5秒；
该物化步骤显式使用60秒截止时间，仍在每个文件及分块边界检查取消；调用取消会通知并回收读取
线程。步骤不创建模型Turn、不自动重试，也不把本地文件系统上的协作截止时间表述为可中断任意
内核I/O的硬实时保证。

成功脚本路径实际包含：`run_tests(fail) → read_file → apply_patch → run_tests(pass) → git_status → git_diff → JSON answer`。可见测试只覆盖目标行为；身份缺失、真实ID/名称漂移和非法类型继续由模型不可见的最终回归检查裁决。

## 4. 正式契约与持久化

新增`harnessix.coding-eval-run-state/v1`，字段包括：

- run、任务版本/指纹和物化基线身份；
- 受管执行副本ID；
- `ready | running | completed`生命周期；
- Thread/Turn ID；
- 基线检查摘要；
- Provider、模型、平台和隔离环境标识；
- 固定报告文件名、报告SHA-256、开始及更新时间。

状态不保存绝对路径、Prompt、模型正文、测试正文、Diff、Action私有指纹或凭据。`run-state.json`最多256 KiB，使用0600临时文件完整写入、`fsync`、同目录原子替换和目录同步；读取使用`O_NOFOLLOW`，拒绝非普通文件、权限放宽、损坏或超限内容。

`ready`不能绑定Session或报告；`running`必须绑定Thread且尚无报告摘要；`completed`必须绑定Thread、Turn和报告摘要。Agent失败、取消或中断属于已完成评分运行，最终仍以`completed`状态发布，其报告结论通常为`failed`；运行状态不复制另一套业务失败分类。

## 5. 审批与安全边界

自动评测审批不是绕过审批。每次Process和Patch仍生成原正式计划、请求指纹与持久审批记录，批准者固定为`harnessix-eval-runner`。编排器在答复前重新读取持久Turn并执行窄白名单：

- Process ToolCall必须是`run_tests`，参数只能包含任务声明的Profile；
- Patch ToolCall必须是`apply_patch`，完整计划中的路径必须属于`allowed_changed_paths`；
- 通用审批、批量Patch、任意Process、未知Profile和额外参数均拒绝。

Process批准只把唯一Action推进到READY，不在Agent Runtime中执行。`ActionWorker.run_once`领取运行私有Effect Journal中的Action；若Action已由上次Worker处理，编排器只观察原终态，不创建第二个Action。Patch继续由副本账本和Session审批双重归因，不能直接写文件。

本片没有OS Sandbox、网络隔离或任意第三方仓库安全执行能力。固定历史检查仍拥有宿主服务账户权限，只能用于Catalog中经过评审的Harnessix自身任务。

## 6. 失败、取消与恢复

运行恢复以三个既有账本和一个运行锚点共同完成：

| 中断位置 | 重开行为 |
|---|---|
| 受管副本完成、状态尚未发布 | 保守拒绝接管已有副本，不覆盖未知目录 |
| `ready`发布后、Thread前后 | 从私有Session索引恢复唯一空Thread，或创建一次 |
| Turn接受后、状态尚未绑定Turn | 以固定`request_id`重取原Turn，不重复接受 |
| Process批准后、Worker前 | 恢复WAITING_ACTION，由Worker消费原READY Action |
| Worker终态后、Session观察前 | 不重跑命令，只把原Action结果投影到Session |
| Patch审批或效果提交窗口 | 复用原Managed Patch恢复和效果核对语义 |
| 调用方取消 | AgentRuntime持久化CANCELLED；本次不强行运行最终检查，后续重开按终态评分 |
| 报告发布后、completed状态前 | 读取并核对原报告，再只补状态和报告摘要 |

运行状态、任务、物化清单、执行副本、Session索引、环境或报告任一身份不匹配均fail closed。启动器正文与0700权限在重开时只验证、不改写，避免`ctime`变化导致Process绑定指纹漂移。已放宽权限、符号链接或正文变化的启动器必须拒绝，而不是就地修复后继续消费旧许可。

## 7. 可观测性与部署

编排器沿用Agent Runtime、Action Service和Worker的跨度、提交/审批/消费/恢复计数以及队列指标；最终报告保存模型步骤、Token、工具调用、审批、修改文件和总耗时。`run_id`、任务指纹、Thread/Turn、Action与Patch身份分别留在其受控持久层，公开报告不携带内部执行许可。

每个运行目录包含：

```text
materialization.json  固定来源与物化身份
run-state.json        编排恢复锚点
report.json           最终脱敏评分报告
workspace/            只读历史物化层
managed/              Patch副本及账本
session.sqlite        Agent事件与投影
effects.sqlite        Process Action事实
host/python           固定解释器启动器
```

目录必须由单一服务账户以0700持有，不得位于来源仓库内部。运行中的同一副本和Session已有进程锁，第二宿主不能并发接管。

## 8. 验收与限制

自动测试直接使用固定真实历史revision，证明完整成功链路、三个正式审批、两次外部Worker测试Action、单一允许Diff、隐藏行为/回归通过、报告与状态原子发布、已完成运行零Provider重放。另覆盖Process批准后退出恢复、取消持久化后零模型重发、报告已发布但状态未提交的恢复，以及启动器身份稳定和权限漂移拒绝。

确定性脚本Provider只证明Runtime基础设施和评分器能够驱动真实缺陷，不形成任何模型成功率结论。0.5.5c必须在显式费用预算下对同一任务执行多次真实Provider试验，并分别统计Provider失败、Runtime失败、任务失败、Token、时延和费用。源目录变更包及显式合入仍属于0.5.5d。
