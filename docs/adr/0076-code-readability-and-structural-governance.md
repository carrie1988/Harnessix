# ADR 0076：代码可读性、可维护性与结构治理

- 状态：已接受
- 日期：2026-09-11
- 决策范围：Harnessix Code 0.9.0

## 1. 背景

0.8完成后，Harnessix已有253个Python源码文件、55,146物理行、273个静态公共导出和多个跨
Session、Process、Workspace及外部副作用的恢复边界。起始基线仅88个模块有文档字符串，
56个公共行为入口和25个高风险入口中的22个缺少邻近语义说明；14个文件超过600行，一级包
导入图存在一个11包强连通分量。

只批量增加逐行注释会制造重复信息，只按行数拆文件可能把共享状态变成隐式耦合，直接启用全局
严格Docstring或复杂度阈值又会迫使既有代码生成模板化说明。项目需要一个能记录现状、优先清理
高风险债务并阻止继续退化的正式机制。

## 2. 决策

### 2.1 机器可读报告

采用`scripts/readability_report.py`生成`harnessix.readability-report/v1`。报告只静态读取
`src/harnessix`，包含：

- 源码文件、物理行、逻辑行、模块/类/函数文档字符串统计；
- 静态`__all__`公共面；
- 公共Pydantic/Enum合同与公共行为入口的区分；
- 25个高风险状态机、副作用及恢复入口；
- 超过600行的文件；
- 超过100行或决策复杂度大于20的符号；
- `harnessix`一级包依赖边和强连通分量。

起始事实固定在`docs/baselines/readability-0.9.0-start.json`，最终接受事实固定在
`docs/baselines/readability-0.9.0-final.json`。报告使用仓库相对路径和确定排序，不包含生成
时间、主机路径或当前分支名称。

这些阈值是评审触发器，不是质量评分。阈值内代码仍可能职责混乱，阈值外代码也不会未经行为
分析被自动拆分。

### 2.2 渐进防退化策略

`governance/readability-policy-v1.json`保存已接受预算。`--check`同时检查并一次报告全部违例：

1. 所有生产源码模块必须有职责说明；
2. 所有静态公共行为类和函数必须有语义说明；
3. 配置的高风险入口必须说明失败、恢复或副作用边界；
4. 不允许新增超大文件或超长/高复杂度符号；既有热点不得超过各自已接受行数和复杂度；
5. 不允许新增一级包依赖边或新的强连通分量；
6. 静态`__all__`变化必须显式更新策略并接受公共契约评审；
7. 最终报告必须与当前源码逐字等价。

策略文件不是自动批准器。需要增长热点、改变公共面或新增依赖时，必须先通过对应ADR或详细设计
解释原因，再人工更新预算；禁止把“重新生成策略”作为修复门禁的方法。

### 2.3 简体中文说明规范

生产代码中的新增职责说明和设计注释使用简体中文，协议名、代码标识符和行业术语保留原文。

- 模块：说明负责什么、不负责什么；关键外部依赖由导入和正式设计文档共同表达；
- 类：说明生命周期、所有权、并发、持久化或协议角色；
- 函数：公共行为及高风险入口说明前置条件、副作用、幂等性、失败和恢复语义中适用的部分；
- 变量：Hash/Fingerprint/Revision、Cursor/Sequence、Attempt、Lease/Timeout/TTL、前后镜像和
  Effect事实优先使用带作用域的名称及类型；只有无法由名称表达的不变量才增加邻近注释；
- 行内注释：解释设计原因、安全边界或反直觉行为，不复述赋值、循环和显然分支；
- 文档：跨模块约束链接正式ADR或详设，禁止把开发过程、聊天记录或临时推测写入代码。

数据合同是例外：Pydantic会把类文档字符串写入JSON Schema的`description`。0.9.0不得为提高
文档字符串指标而改变已发布Schema。因此公共Pydantic/Enum合同由模块职责、类型化字段、模型
校验器、冻结JSON Schema和正式设计文档共同说明；其类文档字符串只能在独立Schema兼容评审中
增加或修改。

### 2.4 Reducer结构边界

把原`agent/reducer.py`拆为：

| 模块 | 职责 |
|---|---|
| `agent/reducer.py` | 稳定门面、Thread级事件分派、`apply_event`和`replay` |
| `agent/item_reducer.py` | Item开始/完成校验及不可变Turn投影 |
| `agent/turn_reducer.py` | Turn状态、模型尝试和Context事件投影 |
| `agent/reducer_support.py` | 共用查询、错误守卫和Process Action一致性验证 |

既有`harnessix.agent.reducer`中的`apply_event`、`replay`、`get_turn`、`pending_calls`和`require`
导入路径继续有效。在线Session提交与离线Replay仍调用同一个`apply_event`；新模块不访问数据库、
不执行Tool，也不产生外部副作用。

拆分不改变Agent Event、Thread/Turn/Item Schema、错误码、事件顺序或状态转换。拆分前后的Agent、
Context、Provider、Tool、Patch和Session回归必须等价；`tests/governance/test_readability_policy.py`
固定门面导入。

### 2.5 暂不拆分AgentRuntime

`AgentRuntime`同时协调Session CAS、取消、审批、Compaction、Provider流、Tool执行与未知效果恢复。
这些职责值得继续治理，但当前方法共享大量端口、Thread锁和中间状态。0.9.0先完成高风险入口
说明，并把`agent/runtime.py`及其热点行数和复杂度冻结在最终预算。

后续提取必须先定义协作者输入输出、所有权和取消传播，保持`AgentRuntime`公共Facade；每次只
提取一个内聚职责，并使用完整Transcript等价测试。不得为了降低行数把`self`状态复制到多个
Mixin，或通过循环导入和Service Locator隐藏耦合。

### 2.6 CI和开发入口

`make readability`运行报告、策略和最终快照检查，`make check`在Ruff、Mypy及pytest之外包含该
门禁。Linux Python 3.12/3.13、macOS和Windows任务显式运行同一命令，避免平台路径或Tokenizer
差异只在单一Runner暴露。

### 2.7 终态提交与Heartbeat判定

异步Journal的终态事务可以先于`transition()`调用返回完成：事务已经提交并清除租约时，执行
协程仍可能等待结果读取或连接关闭。此窗口内`renew_lease()`按合同返回`false`，但它只表示租约
没有续期，不能单独证明执行所有权在终态提交前丢失。

`ActionWorker`因此在续租失败后读取同一Action的持久状态，并在取消本地执行后再次消解并发窗口。
若Journal已处于`TERMINAL_ACTION_STATUSES`，持久终态胜出并作为结果返回；`UNKNOWN`只有在最新
事件为`execution_completed`时才代表Executor已经提交，`lease_recovered`产生的`UNKNOWN`仍按真实
租约丢失处理。取消后再次读取仍无执行完成事实时，才记录续租失败并抛出
`WorkerLeaseLostError`。RUNNING租约过期后的保守恢复、Effect Journal状态机和公共错误合同均不
改变。该判定不重试Tool，也不依据内存任务状态覆盖持久事实。

## 3. 备选方案

### 3.1 全局启用Pydocstyle并要求所有符号有文档字符串

未采用。大量私有小函数和Pydantic数据合同会产生模板化说明；后者还会改变JSON Schema。规则
应优先保护公共行为和高风险边界，而非追求机械覆盖率。

### 3.2 只使用Ruff复杂度阈值

未采用。单一全局阈值会让148个既有热点同时失败，无法区分新增退化和存量债务，也不覆盖模块
职责、公共API、文件规模或包依赖。

### 3.3 立即拆分AgentRuntime和全部超大文件

未采用。大范围目录移动会同时改变导入、状态所有权和恢复调用链，Diff难以评审，也违背纵向
切片和行为保持原则。

### 3.4 只保存百分比预算

未采用。总文件数增加可稀释债务百分比，复杂度平均值也会掩盖单个高风险入口。策略使用精确
路径、符号、依赖边和公共面快照。

## 4. 后果

正向后果：所有生产模块和公共行为有可定位说明；核心副作用与恢复入口可在邻近代码读取边界；
Reducer职责更清晰；新增结构债务会在CI中失败；起始和最终事实可独立复算。

代价：策略文件包含存量热点和164条一级包依赖，评审时需要理解更新原因；静态AST无法发现动态
导入、运行时猴子补丁或真实认知复杂度；增加注释会提高物理行数，因此最终热点预算是在说明
治理后冻结，而非简单沿用起始行数。

## 5. 验收条件

1. 起始与最终报告可由固定脚本重建，报告不含主机相关路径；
2. 全部生产模块、公共行为和25个高风险入口通过说明门禁；
3. Reducer稳定导入路径和完整Transcript投影回归通过；
4. 221份JSON Schema与实现一致，聚合摘要保持
   `1681d5a8bc11e4389716071a9c45cec71cfa3c02d717d4d7f93659fe2f842378`；
5. Ruff、Mypy、全量pytest、离线示例和六矩阵CI全部通过；
6. README、总体架构、测试规范、路线图和0.9.0详设同步。
