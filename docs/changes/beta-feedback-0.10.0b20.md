---
doc_type: change-design
status: current
version: 4
code_revision: d387a5cddf49c25e84eb9ab75cde2abeb4465dab
owners:
  - core
modules:
  - trusted_actions
  - agent
  - product_config
  - models
  - tools
related_adrs:
  - docs/adr/0142-stale-approved-plan-fails-without-effect.md
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md
  - docs/adr/0123-provider-service-failure-attribution.md
  - docs/adr/0145-whole-repository-search-on-large-repositories.md
related_tests:
  - tests/trusted_actions/test_router.py
  - tests/delivery/test_trusted_action_patch.py
  - tests/product_config/test_stale_approved_plan.py
  - tests/product_config/test_parent_closure_product_sdk.py
  - tests/product_config/test_server_and_cli.py
  - tests/agent/test_model_attempts.py
  - tests/context/test_pressure_window_candidates.py
  - tests/tools/test_whole_repository_search.py
  - tests/product_config/test_service_failure_retries.py
supersedes: []
---

# Beta反馈（0.10.0b20真实仓复测）改进设计

## 1. 文档摘要

本文记录0.10.0b20真实仓库复测（2026-11-12）确认的问题中由Beta改进会话负责的部分：

| 项 | 用户看到的现象 | 处理 | 决策记录 |
|---|---|---|---|
| 一 | 批准后整轮中断，显示“存在未知效果”，获批操作没有执行（B20-02） | 过期计划以确定的失败结束，本轮继续 | [ADR 0142](../adr/0142-stale-approved-plan-fails-without-effect.md) |
| 二 | 一次流式中断结束了进行到第57次请求的任务，要手动“继续” | 付费交互模式下服务方故障连续至多自动重试5次 | [ADR 0144](../adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md) |

| 三 | 大仓库在根目录搜索5秒后超时，没有任何结果（B20-05） | 搜索提速约3.5倍，整仓上限放宽到四倍 | [ADR 0145](../adr/0145-whole-repository-search-on-large-repositories.md) |

第1～14节描述第一项；第二、三项的设计分别集中在第15、16节。后续批次（收窄目录绑定）落地时在本文追加。

## 2. 需求背景

复测报告的最高优先级问题：vLLM仓库（6,682个文件）中，用户批准新建测试文件的修改后，界面显示“审批：已批准”，
随即整轮以`uncertain_effect`中断；核心改动还在，获批的文件没有写入，用户要手工核对工作区再续做。

确认过程：

- 会话事件：审批请求12:20:49发出，12:22:40批准，34毫秒后出现`uncertain_effect`
  “触发原因：execution_plan_stale”，没有任何写入。
- 审批期间（约111秒）同一工作区根目录多了一个与本次修改无关的日志文件。
- 在0.10.0b20的真实终端界面复现：让模型新建`notes/hello.txt`，审批窗口打开时在根目录放一个无关文件，批准后
  显示“已中断”，文件没有写入。把修改换成一条命令（`sleep 2 && echo …`）结果相同。
- 确定性探针：对“新建`tests/tools/test_x.py`”绑定的资源捕获Snapshot后，在根目录或`tests/`下新建任意文件，
  复核即报`execution_plan_stale`。

因此这不是并发测试才有的偶发现象：审批期间工作目录或目标上级目录里出现任何文件都会触发。

## 3. 设计目标与非目标

目标：

- 批准后确定没有执行的操作如实报告为“未执行、无改动”，本轮继续；
- 不放宽“旧批准只对批准时的事实有效”这条约束；
- 补丁、命令及其他经Router执行的操作行为一致。

非目标：

- 不在本版收窄Snapshot的绑定范围（无关文件仍会使计划过期，另见ADR 0143的后续批次）；
- 不改变进入Executor之后的未知效果处理；
- 不提供“自动按当前内容重做并沿用旧批准”。

## 4. 总体架构

改动只在Trusted Action Router的执行入口及其结果投影：

```mermaid
flowchart LR
    Approve[用户批准] --> Execute[Router.execute]
    Execute --> Verify{执行前复核Snapshot}
    Verify -- 一致 --> Claim[Claim Operation并调用Executor]
    Verify -- 已变化且Route为ready --> Fail[ready到failed 写审计事件]
    Fail --> Result[失败工具结果 未执行]
    Result --> Model[模型按当前内容重新发起]
    Model --> Plan[新计划 新审批]
```

Agent Runtime、Session事件类型、Workspace Snapshot和各Executor都没有改动。

## 5. 接口设计与数据结构

- `trusted_actions/contracts.py`：`ALLOWED_ROUTE_TRANSITIONS["ready"]`由`{running}`变为`{running, failed}`；
  新常量`STALE_PLAN_CODE = "execution_plan_stale"`。
- `trusted_actions/operation_router.py`：`execute_action`捕获执行前复核抛出的`KernelError`，交给
  `_reject_stale_plan(router, plan_id, error)`；后者只在错误码是`STALE_PLAN_CODE`且Route为`ready`时迁移并返回
  `ActionExecutionOutcome(kind="failed", error_code=STALE_PLAN_CODE)`，否则返回`None`，原异常继续抛出。
- `trusted_actions/public_outcomes.py`：`STALE_PLAN_CODE`登记为公共失败码，避免被归一为`action_failed`。
- `trusted_actions/agent_gateway_output.py`：`build_result`按错误码选择固定说明（`_FAILURE_MESSAGES`）；未登记的
  失败码沿用原通用说明。

审计事件、工具结果和公开协议的字段都没有变化；只是出现了新的状态迁移和一个此前不会出现在工具结果里的错误码。

## 6. 流程与数据流

1. 模型发起写操作，Router规划并冻结Snapshot，进入`pending_approval`。
2. 用户批准，Route进入`ready`。
3. `execute`复核Snapshot。事实未变：Claim Operation，`ready → running`，调用Executor（原流程）。
4. 事实已变且Route为`ready`：一次CAS把Route迁到`failed`，审计事件记录`error_code=execution_plan_stale`；
   不创建Operation，不调用Executor。
5. Gateway生成失败的工具结果（`outcome=failed`、`trusted_action.state=failed`、固定说明），写入Session。
6. 模型在下一步看到失败原因，重新读取并重新发起；新的调用得到新的计划和新的审批请求。

## 7. 失败、恢复、取消与超时

- 复核失败但Route不是`ready`（尚未批准、执行中、已是终态）：原异常照旧抛出，行为不变。
- CAS迁移时Route被并发改变：`action_route_conflict`照旧抛出，不吞掉。
- 迁移完成后进程退出：恢复时Route已是`failed`终态，Gateway的恢复路径按原逻辑重新投影同一条失败结果，不会执行。
- 迁移之前进程退出：Route仍是`ready`，恢复路径不启动效果（原逻辑），Turn按原规则终结。
- 取消与超时路径没有改动。

## 8. 持久化与数据流程

- Action审计库多出一种事件序列：`… → ready → failed`（没有对应的Operation行）。
- Workspace事务库中该计划的事务保持`prepared`，不会被发布；沿用原有的保留与清理规则。
- 没有Schema变更，没有迁移脚本。

## 9. 安全与信任边界

- 旧批准只能用于批准时冻结的事实：过期计划成为终态，不能再执行，也不会在工作区恢复旧状态后悄悄生效。
- 不在执行阶段刷新Snapshot，新的事实必须经过新的规划和审批。
- 说明文本是固定常量，由错误码选择，不含路径、参数或异常正文。
- “没有效果”的结论有依据：Route为`ready`意味着从未Claim过执行，Executor没有被调用。

## 10. 可观测与错误分类

- 审计事件`error_code=execution_plan_stale`、`from_state=ready`可直接统计这类失败的次数。
- 工具结果错误码`execution_plan_stale`，类别为普通失败；Turn不再因此出现`uncertain_effect`。
- 界面显示为该操作的失败行，带固定说明。

## 11. 部署、兼容与回退

- 升级后已有会话不受影响；升级前已批准但未执行的计划，在工作区变化后同样按新规则以失败结束。
- 回退到旧版本：旧版本不会产生`ready → failed`；读取含这条迁移的审计链不受影响（迁移合法性只在写入时检查）。
- 三个平台行为一致，没有平台相关代码。

## 12. 限制、风险与取舍

- 无关文件仍会使计划过期（绑定范围未变），用户会多批准一次；有程序持续向工作目录写文件时可能反复出现。
  这是本批次有意留下的，由收窄绑定的后续批次解决。
- 模型可能不重新发起而是先做别的检查，这由模型决定；真实运行中两个场景都在同一轮内重新发起并完成。
- 进入Executor之后才发现的变化（复核与发布之间的极短窗口）仍按写类操作的保守规则记为未知，再由对账确认未执行。

## 13. 验证

- 单元与集成测试：Router（过期计划返回确定失败、不调用Executor、不创建Operation、旧计划不可再执行、重新规划后
  正常执行）；补丁Executor；产品组合下的两条端到端用例（目标被外部改写；根目录出现无关文件后模型重新提交并在
  第二次审批后发布）；SDK路径两条既有用例改为断言新行为。
- 全量测试与0.10.0b20基线逐项对比：除上述改动的用例和可读性基线外没有差异。
- 真实终端界面（百炼qwen3.8-flash）：补丁审批、命令审批两个场景，0.10.0b20均为“已中断”；修复后均为“已完成”，
  失败行显示固定说明，第二次审批后操作执行。

## 14. 源码与测试映射

| 内容 | 源码 | 测试 |
|---|---|---|
| 状态迁移与错误码常量 | `src/harnessix/trusted_actions/contracts.py` | `tests/trusted_actions/test_router.py` |
| 执行前过期的确定失败 | `src/harnessix/trusted_actions/operation_router.py` | `tests/trusted_actions/test_router.py`、`tests/delivery/test_trusted_action_patch.py` |
| 失败码登记与说明 | `src/harnessix/trusted_actions/public_outcomes.py`、`agent_gateway_output.py` | `tests/product_config/test_stale_approved_plan.py` |
| 产品端到端 | — | `tests/product_config/test_stale_approved_plan.py`、`test_parent_closure_product_sdk.py`、`test_server_and_cli.py` |

## 15. 第二项：交互模式下服务方故障后的自动重试

### 15.1 背景与决定

复测的OCR任务第57次模型请求时流式响应中途断开，该次用量没有报回。按[ADR 0123](../adr/0123-provider-service-failure-attribution.md)
第6节，付费端点的交互模式在“计费未知”时停止，整轮以`provider_transport`结束，用户输入“继续”后才接着做。
同一轮里另外两次连接失败因为确定未计费而自动重试成功。产品负责人2026-11-12决定：网络中断允许自动重试，
重试次数为5次（[ADR 0144](../adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md)）。

### 15.2 设计

```mermaid
flowchart TD
    Fail[模型尝试失败 用量未知] --> Kind{服务方故障?}
    Kind -- 否 --> Stop[停止 交给用户]
    Kind -- 是 --> Count{最近连续此类失败不超过5次?}
    Count -- 否 --> Stop
    Count -- 是 --> Reserve[按预留额记账]
    Reserve --> Budget{预留后仍在Token预算内?}
    Budget -- 否 --> Stop
    Budget -- 是 --> Next[发起下一次请求]
```

- `agent/provider_stop.py::service_failures_within(turn, limit)`：本Turn用量未知的尝试（含摘要请求）必须全部是
  服务方故障（`provider_transport`、`provider_rate_limit`、`provider_provider_internal`），且主模型尝试末尾连续的
  此类失败不超过`limit`。
- `agent/turn_budget_compaction.py::UnknownUsagePolicy(always, service_failure_retries)`：`continues(turn)`为真时
  按预留额继续（`require_reserved_unknown_usage`），否则沿用原停止规则。
- `AgentRuntime`的参数`continue_after_unknown_usage: bool`改为`unknown_usage: UnknownUsagePolicy`，四个读取点
  （请求额度、摘要额度、每步发起前、预算压力压缩规划）改为调用`continues(turn)`。默认策略仍是停止。
- `product_config/server.py`：默认值`SERVICE_FAILURE_RETRIES = 5`；无人值守和不计费端点`always=True`（行为不变），付费
  交互模式`always=False`，次数取配置项。
- 配置项：`ProductConfigV2.service_failure_retries`（0～20，未设置即默认5，不写入配置文件；0表示不自动重试），
  `harnessix code configure --service-failure-retries N`经`ConfigurationDraft`写入。
- 向导生成配置的`max_attempts`由4改为6；`ModelProfile.max_attempts`与`ModelHTTPConfig.max_attempts`的上界由5
  放宽到6，7个公开Schema的`maximum`随之更新。

### 15.3 边界

- 响应已经开始输出之后才断开的请求，适配器不重试（ADR 0123未改变的部分），该轮仍以原故障结束。
- 非服务方故障造成的用量未知（输出无效等）照旧停止。
- 连续6次失败后停止，错误码与说明和原来相同；中间有一次成功则重新计数。
- 重试的请求可能已被计费，最坏多付5次请求的费用；预留额仍计入Token预算。

### 15.4 验证

- `tests/agent/test_model_attempts.py`：三类服务方故障各自重试后本轮完成；连续5次失败后第6次成功照常完成，
  连续6次失败后停止且第7次请求没有发出；其他原因的用量未知仍停止；策略参数校验；原有“默认停止”
  “无人值守继续”用例保持通过。
- `tests/context/test_pressure_window_candidates.py`：预算压力压缩规划读取同一策略。
- `tests/product_config/test_service_failure_retries.py`：真实产品服务端装配下，默认配置与配置为3时中断后重试并完成，
  配置为0时立即以`provider_transport`结束。`tests/product_ui/test_configure_vendors.py`：configure参数的写入与越界拒绝。

## 16. 第三项：大仓库的整仓搜索

### 16.1 背景与确认

复测在vLLM仓库（6,682个文件、101.6 MB）根目录搜索`report_build_time_ninja`，5秒后`tool_timeout`，没有结果，
报告记为单次观察。用同一份仓库副本直接调用产品的搜索函数复核：默认时限下必然超时；解除时限后读到64 MiB
上限即`limit_exceeded`；同一查询`git grep`用0.36秒。分析显示约六成时间在逐字符的二进制判断，约四分之一在
对不含查询的文件逐行调用匹配器。

### 16.2 设计

- `tools/files.py::_decode`：控制字符判断由逐字符循环改为一次预编译正则扫描，结论不变。
- `tools/grep_matcher.py::whole_text_decides(args)`：快照与正则搜索中，字面量查询和不含`^`、`$`、`\A`、`\z`的
  正则可以先对整份文本判断一次。
- `tools/search.py::grep_line_matches`：上述条件成立且整份文本不命中时直接返回，不逐行扫描、不计算摘要；
  超长行的字节数判断先比较字符数（UTF-8每字符至多4字节），多数行不必编码。
- `tools/search_contracts.py`：快照与正则搜索的条目、名称字节、总读取上限放宽到四倍；`SnapshotSearchStats`
  的字段上界随之放宽，两个公开Schema更新。
- 执行合同版本：快照v5→v7，正则v6→v8；原字面量合同v3及其描述符摘要不变。

### 16.3 行为差异

- 整份文本都不含查询的文件，其超长行不再计入`long_lines`（它们藏不住命中），这类搜索可以报告
  `scan_complete`。可能命中的文件照旧计数。原字面量合同的统计口径不变。
- 不含查询的文件不再计算正文摘要；命中结果里的摘要、修订号和上下文与原来相同。
- 读取时限（5秒）、单文件上限（2 MiB）、忽略目录、路径安全检查都没有变化。

### 16.4 验证

- `tests/tools/test_whole_repository_search.py`：二进制判断对0～0x17F的每个码点及若干非ASCII字符与原规则
  逐一比对；捷径只在不会漏行的查询上启用；含锚点与不含锚点的查询在CRLF与LF文件上命中相同的行；
  不含查询的20个文件各只判断一次；超长行计数的新口径与原字面量合同的旧口径；上限数值。
- 既有搜索测试（POSIX与Windows内存端口两套）全部通过；描述符摘要测试更新快照合同的摘要，原字面量合同的
  摘要保持不变。
- 真实仓库：vLLM副本、产品默认时限与上限。`report_build_time_ninja`由5.00秒超时变为2.46秒返回7处命中；
  其余三类查询见ADR 0145的表格。

### 16.5 未解决

更大的仓库仍会超时且没有部分结果（需要改搜索输出合同）；每个文件重新逐级打开并核对根身份的开销未动；
Glob仍用原上限。
