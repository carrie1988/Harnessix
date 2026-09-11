# 0.9.0代码可读性、可维护性与结构治理详细设计

- 状态：候选实现，待六矩阵验收
- 适用版本：Harnessix Code 0.9.0
- 决策依据：[ADR 0076](adr/0076-code-readability-and-structural-governance.md)
- 研究依据：[代码可读性与结构治理研究](research/code-readability-and-structure.md)

## 1. 目标与边界

0.9.0建立能长期执行的源码治理基线，并完成一个行为保持的核心结构切片。交付目标为：

- 生产源码模块、公共行为和高风险副作用/恢复入口均可就近理解；
- 文件、符号、一级包依赖、依赖环和公共导出有可复现事实；
- 存量热点被逐项冻结，新代码不能扩大未评审债务；
- Agent Reducer按领域职责拆分，公共门面及持久事件语义不变；
- 文档、测试和CI使用同一治理口径。

非目标包括：重写Agent Loop、改变数据合同、一次性消除全部依赖环、把所有私有函数加注释、
调整Provider行为、引入新的代码质量第三方依赖，或仅为行数拆分`AgentRuntime`。

## 2. 交付物

| 交付物 | 路径 | 作用 |
|---|---|---|
| 起始事实 | `docs/baselines/readability-0.9.0-start.json` | 固定0.9.0起始债务 |
| 最终事实 | `docs/baselines/readability-0.9.0-final.json` | 当前接受的完整静态报告 |
| 治理策略 | `governance/readability-policy-v1.json` | 保存只减不增预算与公共面 |
| 报告器 | `scripts/readability_report.py` | 生成、比较和返回CI退出码 |
| 治理测试 | `tests/governance/test_readability_policy.py` | 防止报告、策略和Reducer门面漂移 |
| 研究与决策 | `docs/research/...`、`docs/adr/0076-...` | 记录事实、取舍与适用边界 |

## 3. 报告器设计

### 3.1 输入

默认输入为`src/harnessix`。`--source-root`允许在临时导出的固定提交上复算起始基线；
`--revision`只作为该离线报告的来源标识，不自动读取Git，避免未提交状态和分支名称影响结果。

读取约束：

- 只读取`.py`文件并按路径排序；
- UTF-8解码或AST语法错误直接失败，不跳过文件；
- 不导入Harnessix生产模块，不加载插件、配置、数据库或环境变量；
- 报告路径规范化为`src/harnessix/...`，不保存绝对路径。

### 3.2 AST与Tokenizer投影

每个模块投影为`ModuleSource(name, path, text, tree)`。Tokenizer统计非注释逻辑行；AST统计模块
文档字符串、顶层类、顶层函数和类方法。函数内嵌套定义不计入符号总数，复杂度也不穿透嵌套
函数、类或Lambda，以避免同一分支被父子符号重复计数。

决策复杂度是稳定启发式值：

```text
1
+ if / for / async for / while / 条件表达式
+ 推导式生成器及其每个过滤条件
+ BoolOp额外操作数
+ except / try-else / finally
+ 非默认match case
```

### 3.3 公共面识别

报告器只接受静态字符串`__all__`。动态拼接、非字符串成员或不可解析表达式直接失败。导出名称
沿绝对`from ... import ...`静态解析到顶层类或函数；常量仍保留在公共名称快照，但不要求
函数文档字符串。

类的基类链若静态到达`BaseModel`、`RootModel`、`Enum`、`IntEnum`或`StrEnum`，分类为公共合同
类型。合同语义由类型字段、校验器和生成Schema承担；其他导出类与函数分类为公共行为，必须有
文档字符串。该分类避免注释治理无意修改Pydantic JSON Schema描述。

### 3.4 高风险入口

报告器维护25组稳定职责入口，覆盖：

- `apply_event`、`replay`、Item开始和Turn状态迁移；
- `AgentRuntime`接纳、CAS提交、恢复、审批、Compaction、循环、Tool、模型流和终态；
- Workspace事务发布/对账，Git Worktree/Checkpoint/Commit，受管Patch；
- Process启动和Owner回执刷新；
- Trusted Action规划与单次执行。

Reducer结构迁移组允许旧、新符号二选一，因此固定起始提交和最终代码都能使用同一指标版本。
每组必须且只能找到一个当前入口；全部候选缺失时报告器失败，防止重命名绕过门禁。

### 3.5 依赖图

一级包依赖只统计绝对`harnessix...`导入，不统计标准库、第三方包和同包边。Tarjan算法生成排序
稳定的强连通分量。静态延迟导入仍会被`ast.walk`发现；字符串动态导入不在本版本覆盖范围内，
新增动态导入必须由代码评审单独拒绝或说明。

## 4. 策略校验

策略从人工接受的最终报告生成，但日常CI只读取，不自动更新。校验按一次执行聚合全部错误：

| 检查 | 失败条件 | 修复方式 |
|---|---|---|
| 模块说明 | 任一源码模块无Docstring | 补充职责和非职责说明 |
| 公共行为 | 任一导出行为类/函数无Docstring | 说明合同、副作用或生命周期 |
| 高风险入口 | 配置入口无失败/恢复说明 | 补齐邻近语义并复核实现 |
| 超大文件 | 新文件超过600行或既有文件增长 | 提取真实职责，或通过ADR调整预算 |
| 热点符号 | 新符号超过100行/复杂度20，或既有值增长 | 简化控制流或通过ADR调整预算 |
| 包依赖 | 出现未批准一级包边 | 反转依赖、复用端口或评审新增边 |
| 依赖环 | 出现新强连通分量 | 调整模块所有权，不以动态导入规避 |
| 公共面 | `__all__`快照变化 | 完成公共契约和兼容评审后更新 |
| 最终报告 | 当前报告与冻结文件不同 | 判断是回退、治理还是需评审更新 |

错误输出不包含源码正文、环境变量或凭据。失败退出码为1；成功为0。

## 5. Reducer详细结构

### 5.1 稳定门面

`agent/reducer.py`继续拥有`apply_event`和`replay`。Session Store仍通过该模块执行在线投影和离线
重建；`get_turn`、`pending_calls`和`require`使用显式同名再导出，Mypy和既有调用点无需修改。

### 5.2 Item投影

`agent/item_reducer.py`负责：

- 用户消息、Steering、Plan、Text、Tool Call、Tool Result、审批、提问和Process状态Item准入；
- Item ID、Call ID、步骤、指纹、审批、效果和模型历史闭合性检查；
- Item开始态与完成态的不可变`Turn.model_copy`。

它只依赖模型、纯查询和校验Helper，不访问Session Store或Executor。

### 5.3 Turn投影

`agent/turn_reducer.py`负责：

- `TURN_TRANSITIONS`和取消/失败/中断迁移；
- 进入模型、Tool、审批、提问、Action等待和终态前的开放事实检查；
- 模型尝试与Usage结算；
- Context检查和Tool Result模型历史决定。

任何非法迁移继续以`KernelError("invalid_event", ...)`失败，终态不可重开。

### 5.4 共用守卫

`agent/reducer_support.py`集中`require`、Turn查找、待处理Call查询及Process Action一致性校验。
Process结果必须绑定匹配审批、Action身份和最后效果状态；UNKNOWN及取消边界继续保守处理。

### 5.5 调用流程

```text
SessionStore.append / rebuild
        │
        ▼
agent.reducer.apply_event
        ├─ Thread create/fork/archive与Turn分派
        ├─ item_reducer      ItemStarted / ItemFinished
        ├─ turn_reducer      TurnState / Attempt / Context
        ├─ compaction_reducer
        └─ reducer_support   纯查询与一致性守卫
        │
        ▼
不可变Thread投影
```

拆分中没有新增Schema、数据库、I/O、异步任务或错误翻译层。

## 6. 注释与命名落地

全部256个最终生产模块具有模块说明。静态公共行为债务由56降为0，高风险入口债务由22降为0。
说明优先覆盖Agent、Delivery、Patch、Process、Trusted Action，再覆盖Protocol、SDK、MCP、Skills、
Hooks、Provider、Context和Eval。

公共Pydantic和Enum合同共146类，不批量增加类文档字符串。其字段、模型校验和221份冻结Schema
保持原字节语义；后续若要改善Schema描述，必须作为公共合同变更单独评审。

关键身份继续保留领域后缀，例如`request_fingerprint`、`owner_identity`、`receipt_sequence`、
`approval_fingerprint`、`expected_sequence`、`lease.deadline`和`stop_reason`。不得缩写为`hash`、
`seq`、`id2`或无作用域的`timeout`。

## 7. 失败与恢复语义

- 报告读取或解析失败：立即失败，不发布部分报告；
- 策略文件缺失、损坏或版本未知：立即失败，不使用默认放宽策略；
- 多项违例：一次列出全部已识别项，便于同一变更闭环；
- Reducer非法事件：保持原`invalid_event`错误，Session事务整体回滚；
- Reducer拆分后进程崩溃：恢复仍从同一有序事件日志调用门面`replay`，无新增中间状态；
- Pydantic合同说明：不通过Docstring改Schema，防止旧客户端观察到未声明描述漂移。

## 8. 测试与验收

### 8.1 专项

`tests/governance/test_readability_policy.py`验证：

1. 当前报告等于最终冻结文件，策略无违例；
2. 起始报告绑定固定提交并保留165个缺失模块事实；
3. 模块、公共行为、高风险入口、超大文件、依赖边、依赖环和公共面回退会在一次检查中全部报告；
4. Reducer五个既有门面导入继续有效，`apply_event/replay`仍属于稳定门面。

Reducer拆分前后运行同一组Agent、Context、Provider、Tool、Patch和Session特征测试。最终验收还需：

```bash
make readability
make check
uv run python scripts/generate_specs.py
git diff --exit-code -- spec
uv run python -m examples.kernel_search
# 其余CI声明的离线示例同样执行
```

### 8.2 跨平台

Linux Python 3.12/3.13运行完整Ruff、Mypy、pytest和离线示例；macOS运行Coding Tools组合；
Windows运行治理、执行、Workspace、Sandbox、Secret、Delivery、扩展、配置和Process集合；
PostgreSQL与固定镜像Container任务分别关闭持久化和隔离后端门禁。

## 9. 后续变更流程

1. 运行`make readability`确认变更前基线；
2. 新模块先写职责说明，新公共行为和高风险入口先写语义合同；
3. 若触发文件、符号、依赖或公共面门禁，先判断能否缩小职责；
4. 确需扩大时提交独立ADR、行为测试和策略Diff，不与功能大改混合；
5. 运行`make check`、Schema冻结和相关跨平台测试；
6. 只有评审接受的新事实才更新最终报告和策略，起始报告永久不改。
