---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, models, context, product_config, product_ui, evals]
related_adrs:
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
  - docs/adr/0124-completion-guard-for-unattended-turns.md
  - docs/adr/0126-incremental-session-history-verification.md
related_tests:
  - tests/agent/test_request_notes.py
  - tests/agent/test_compaction_calibration.py
  - tests/models/test_notes_in_history.py
  - tests/context/test_engine.py
  - tests/integrations/test_harnessix_configs.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# ADR 0132 请求只追加，压缩点按真实Token校准

- 状态：评审中
- 日期：2026-11-11

## 1. 背景

SWE-bench Multilingual正式评测（30题×2次，qwen3.8-flash-next，产品提交447fe024）中，Harnessix每次模型调用
27.7秒，mini-swe-agent 15.4秒。其中“模型开始输出前的等待”9.4秒对约4.2秒，典型值5.4秒对约3秒；另有48次
上下文压缩约187分钟，占全场12%。模型服务有前缀缓存：请求与上一请求开头相同的部分不重算，其余部分按约
每1000 Token 1.7秒重算（2026-11-09回放实测）。两处产品机制使要重算的部分变大。

### 1.1 逐请求的末尾说明

`prompt_cache="stable_tail"`（自托管端点的产品默认）下，目录说明与预算说明以末尾`user`消息附在每个请求上，
下一请求去掉旧的、换成新的。缓存因此只能复用到上一条说明之前，模型刚生成的回复也要重算。
gin-4003 md3MRob step 15只新增66个Token的工具结果，却等了19.2秒才开始输出。

核对产品路径后有两点与原先的认识不同：

- **产品路径只附了目录说明，约150个Token。** 预算说明只在Provider对象实现`ModelInputTokenEstimator`时生成，
  产品的`SafeFallbackProvider`不实现它，所以评测里的模型从未收到过预算说明。“两条说明约350个Token”来自
  旧评测宿主（直接使用`OpenAIChatProvider`）上的回放。
- **工具参数会被重新序列化。** Provider把参数解析成对象，重发历史时用紧凑JSON输出：键顺序、取值与
  非ASCII字符都与模型的输出一致（持久化往返后仍一致），模型原文里的空白不保留。

### 1.2 压缩点

压缩点以估算器口径（UTF-8字节）表示，声明模型窗口时按每Token 3字节换算。本次数据里历史字节数与真实输入
Token之比是4.3～6.7（27个发生压缩的试次，中位5.35；估算口径是完整的Item JSON，线上是紧凑文本）。结果：

| 压缩点 | 字节 | 实际触发时的真实输入 | 占262144窗口 |
|---|---|---|---|
| `third_window`（评测专用覆盖） | 262144 | 中位5.3万Token（4.2万～7.1万） | 约20% |
| `half_window`（产品默认） | 393216 | 约7.7万Token | 约30% |

mini从不压缩，上下文最大到9万Token。48次压缩的三笔开销：写摘要100分钟（摘要请求的开头与平时不同，全部
重算）、压缩后首个请求全部重算26分钟、拖慢同时运行的其他试次约60分钟（fmt-2457 tk2eakD压缩的149秒里，
gin-4003 8Moka8R的第1步等了79.5秒）。

## 2. 决策

### 2.1 请求只追加（`AgentRuntime(history_notes=True)`）

显式开启后，宿主说明不再由Provider映射逐请求附加，而由[`request_notes`](../../src/harnessix/agent/request_notes.py)
在需要时落为历史里的一条已完成用户消息Item（与完成守卫、截断恢复的补发提示同一机制，正文以
`[Harnessix host notice]`开头）。Item一旦写入就一直留在历史里，所以每个请求都等于“上一请求＋新内容”。

**预算说明：只在跨过关口时各发一次。**

| 关口 | 条件（步数或累计Token任一满足） | 给模型的建议 |
|---|---|---|
| 一半 | 剩余调用数≤预算的1/2，或剩余Token≤预算的1/2 | 决定完成与验证还需要什么，其余略去 |
| 四分之一 | 剩余≤1/4（同上两维） | 现在完成工作并验证 |
| 最后十次 | 已过四分之一关口，且剩余调用数≤10，或剩余Token按上一请求的实际尺寸只够再发≤10次 | 停止探索，确认工作已落实并检查一次，然后给出最终答复 |

- 说明先写事实（`N of M model calls are left`；Token已用掉一半以上时再写剩余Token与“按当前请求尺寸约还能
  发几次”），再写一句建议。交互模式不设Token上限，说明里不出现Token。
- 剩余调用数含即将发起的这一次，失败重试与主步骤共用步数预算。
- “最后十次”要求先过四分之一关口：步数预算本来就小的任务（如16步）不会在第7步就被催促收尾。
- 一步内跨过多个关口时只发最紧的一条；较紧的关口发过之后不补发较松的。按100步预算，三条说明分别出现在
  第51、76、91次调用之前。
- 身份为`uuid5(turn_id, "harnessix.request-notes/v1:budget:<half|quarter|final>")`，同一关口不会重复发，
  崩溃恢复后重入也不会。

**目录说明：只在可执行集合会变化时发。**

- 没有配置工具视图（产品的通用执行路径、本次评测）时，可执行集合与选择约束整轮不变，请求的`tools`已经
  说明了一切：不发目录说明。
- 配置了工具视图时，每个Turn的首个请求发一条，此后本步的说明正文（可执行别名、受限原因、选择约束）与本Turn
  上一条不同才再发一条。正文写明“自本条起生效，直到更新的宿主说明替换它”。身份为
  `uuid5(turn_id, "harnessix.request-notes/v1:catalog:<序号>")`。
- `stable_tail`下指定函数的`tool_choice`仍改发`required`（ADR未变）；被强制的工具写在持久说明里。

**Provider映射。** `ModelRequest.notes_in_history`为真时，`budget_instruction`与
`current_tool_catalog_instruction`返回空：OpenAI Chat的三种布局（默认、`explicit_tail`、`stable_tail`）与
Anthropic都不再逐请求附加说明；`explicit_tail`的第二个缓存标记落在历史最后一条文本消息上，其后没有易变内容。
开关关闭时（默认）各映射逐字节不变。

**产品接线。** 产品Server在当前Profile链含`stable_prompt_prefix`的Provider时开启（自托管端点的向导默认）。
链上其余Provider沿用同一份历史，按请求标记同样不再附加说明。未声明稳定前缀的商用端点保持原状：它们的说明是
开头的`system`消息，可执行集合不变时内容不变，不破坏缓存。

**与其他机制的关系。**

- 说明只给模型看：界面按身份隐藏（与取消后的宿主说明一致），不显示为“你”的消息。
- 工具视图拿到的Turn副本不含这些说明：视图用“最近一次用户输入”划分阶段，宿主说明不是用户的新输入。
- 压缩把当前Turn的全部用户消息固定保留（ADR 0124），说明因此不会被摘要掉；说明只有几条、每条不到100个Token。
- 回放与校验不变：说明是普通的`ItemStarted`/`ItemFinished`事件（Event v19起允许准备阶段的用户消息），写在
  本步`ModelHistoryPrepared`之前，历史哈希、Reducer重放与增量校验（ADR 0126）都按既有规则覆盖它们；
  不新增事件类型或版本。

### 2.2 历史里的助手消息

目标是历史里的助手消息与模型当时的输出一致，使缓存能一直复用到模型输出的末尾。

- 已经一致的部分：正文原样；工具名用同一别名；参数的键顺序、取值、非ASCII字符不变（新增回归测试固定）。
- 没有保留的部分：参数原文里的空白。vLLM会把历史消息里的参数解析成对象再交给对话模板渲染，客户端发送的
  空白不影响提示词；其他端点如何渲染不可见。**保留参数原文需要给`ToolCallCompleted`与`ToolCallContent`增加字段，
  属于Agent Event公共合同变更，本次不做。**
- 已知仍会不一致的情况：被目录拒绝的调用以固定哨兵重发（不保留名称与参数是既有安全决定）；参数被修复或还原
  之后持久化的是修复后的值；开启思考时不回传推理内容。

### 2.3 评测沿用产品默认的压缩点

`integrations/harbor/harnessix_configs.py`去掉`"compaction": "third_window"`覆盖，`EVAL_ALLOWED_OVERRIDES`只剩
`completion_guard`。评测测到的就是用户拿到的默认配置；累计Token预算在运行规格里放宽（由集成方处理）。
这修订了ADR 0120里“评测显式选择`third_window`”的决定；`third_window`仍是合法的产品配置值。

### 2.4 压缩点按真实输入Token校准（`AgentRuntime(compaction_window=...)`）

声明了模型窗口时，`product_context_budget`另给出真实Token口径的窗口（`CompactionTokenWindow`）。
[`compaction_calibration`](../../src/harnessix/agent/compaction_calibration.py)在每步准备历史时取Thread内最近一次
已完成主请求的两个持久事实：该步Context检查记录的估算字节数`e`（其中历史`h`，工具定义与指令`f = e − h`），
与Provider为同一请求报告的真实输入Token数`u`。以`W`为声明窗口、`O`为输出预留：

```text
输入上限 L = min((W − O − 1536) × e / u, max_input_bytes)
触发点   = max(固定换算的触发点, min(W × e / u / 触发分母 − f, (L − f) × 4 / 5))
目标     = max(固定换算的目标,   min(W × e / u / 目标分母 − f, 触发点 × 触发分母 / 目标分母))
```

- **触发点落在真实窗口的一半。** 触发点比较的是历史字节数，所以扣除`f`：触发时整个请求的真实输入约为`W/2`。
- **请求不超过出站请求容量。** `max_input_bytes = min(W × 8, _REQUEST_INPUT_CAP)`。`_REQUEST_INPUT_CAP`是出站请求
  保护扫描容量的四分之三（786432字节），Planner的输入上限始终不超过它。
- **触发点在输入上限之下留出一次退避的余量（4/5）。** 主动压缩被跳过（规划不成立、摘要被截断）后，历史再增长
  四分之一才重试；触发点留出同样的余量，重试发生时请求仍在输入上限之内，一次失败的压缩不会让Turn以
  `context_budget_exceeded`终止。
- **只放宽，不收紧。** 观测比例低于3（内容更密）时沿用固定换算的数值，超窗仍由Provider超限后的被动压缩兜底。
- **比例上限8字节/Token。** 实测上端是6.7；更高的比例视为用量上报不可信（例如只上报未命中缓存的输入），按8封顶。
- **输入上限同步放宽。** 校准值经`ContextBuildInput.input_limit_tokens`交给Planner，只在大于固定上限时生效；
  摘要来源上限取同一数值。压缩计划记录它实际使用的策略，重放与重开按计划自带的策略校验，既有计划不受影响。
- **行为不变的情况。** 未声明窗口、没有Context Planner、Thread里还没有带完整用量的已完成请求（含Fork出的新
  Thread）：全部沿用固定换算的数值。校准跨Turn取最近一次请求，新Turn的首个请求不会退回固定比例。

**对262144窗口的实际效果受出站请求容量限制。** 该窗口按固定比例换算已经等于容量（786432字节），校准能放宽的
只有触发点在容量内的位置：`(786432 − f) × 4/5`，按工具定义与指令`f`约2万字节估算约61万字节。比例不超过约4.8时
触发点正好在窗口一半；按本次数据的中位比例5.35约在11.8万Token（45%），比例6.7时约9.4万Token（36%）。要让所有比例下都到一半，需要提高
`MAX_SCAN_BYTES`（公开输出保护的安全预算），不在本次范围。

## 3. 没有采用的做法

- **摘要请求沿用平时的请求前缀以命中缓存。** 现在的摘要请求是“摘要指令＋一条包含来源JSON的用户消息”，与平时的
  请求没有公共前缀，全部重算（本次每次约90秒）。可行的做法是：摘要请求发送与下一步完全相同的指令、工具目录与
  历史，只在末尾追加一条“请按以下要求总结到某一条为止的历史”的用户消息，这样只需计算末尾几十个Token和摘要输出。
  摘要来源格式（`harnessix.compaction-source/v1`）的摘要值记录在压缩计划里并参与重放校验，请求形态改变后来源
  摘要值、摘要输入预付（`summary_input_tokens`）与预算压力路径的预付都要跟着重新定义，需要单独的合同变更与
  回归，本次不做。校准后压缩次数预计从48次降到3次左右，这一项的收益也相应变小。
- **所有Provider都开启持久说明。** 商用端点的说明在请求开头且内容稳定，没有缓存问题；改动它们的请求形态缺少
  评测依据。这些路径上模型仍然收不到预算说明（既有缺陷，见§1.1），开启只需把产品Server里的条件改为恒真。
- **Turn开始时就告知预算总量。** 会从第一步起改变模型的做法，需要单独对照。
- **只提高评测的压缩点。** 只改评测配置，产品没有变好。

## 4. 预期效果

以下是按本次Multilingual数据（58个有效试次、3277次模型调用）的估算，没有模型服务，均未实测。

| 项目 | 现状 | 预期 | 依据 |
|---|---|---|---|
| 每个请求多重算的Token | 目录说明约150，加上一步的回复（平均259） | 0（说明不再出现；回复能否复用取决于服务端渲染） | §1.1 |
| 每次调用开始输出前的等待 | 9.4秒 | 少0.25～0.7秒，全场14～38分钟 | 150～410 Token × 1.7秒/千Token × 3277次 |
| 压缩次数 | 48次（27个试次） | 只做§2.3：20次（17个试次）；加上§2.4：3次（3个试次）；没有容量限制时1次 | 逐试次按各自比例模拟，`f`取2万字节 |
| 压缩总开销 | 约187分钟 | 约25分钟（3次，每次因上下文更大约8分钟） | 摘要与压缩后重算都随上下文增大 |

- 等待时间的下限（0.25秒）只算说明本身；上限假设服务端对助手消息的渲染与模型输出逐Token一致。互相拖累
  （一个试次重算大段提示词时拖慢其他试次）减轻带来的收益没有计入。
- 压缩变少后累计输入Token变多：上下文到11.8万Token的试次跑满100步约需800万～900万Token，评测预算要一并放宽。
- 请求变大后宿主每步的固定开销（规范JSON、保护扫描）随历史字节数增加，由步骤开销的工作包单独处理。

## 5. 后果与风险

- 模型不再每步看到“工具使用是可选的……如实结束”的目录说明，对提前收工或坚持调用工具的影响需要在开发集上看。
- 预算说明第一次真正到达产品路径上的模型：可能更早收尾。三条说明的措辞只陈述事实并给一句建议，不设硬性规则。
- 用量上报偏小的端点会把触发点推到容量允许的位置，真实上下文可能先到窗口：此时由Provider超限后的被动压缩处理，
  与校准前内容较密时的情况相同。
- 压缩一次的代价变大（上下文约为原来的两倍），但次数少一个数量级。
- `runtime.py`增加15行（两个构造参数、两处请求字段、三处调用、导入），超出可读性基线，需要集成方调整基线。

## 6. 验证

- `tests/agent/test_request_notes.py`：三个关口的步数与Token判定、小预算、重试计入、每个关口只发一次、只向更紧的
  关口推进；目录说明随可执行集合变化；真实Runtime下相邻请求的历史与`stable_tail`线上消息逐条是前缀关系
  （gin-4003 md3MRob的一般形态），开关关闭时旧行为不变；重放一致；工具视图与界面看不到说明。
- `tests/models/test_notes_in_history.py`：三种布局与Anthropic在标记下不附加说明且请求只追加；默认行为不变；
  持久说明的正文；工具参数的键顺序与取值在持久化往返后不变。
- `tests/agent/test_compaction_calibration.py`：触发点在真实窗口一半；出站请求容量与退避余量；只放宽；
  比例上限；跨Turn观测；无窗口或无观测时配置不变；真实Runtime下“真实输入占窗口20%不压缩、过半才压缩”
  （fmt-2457 tk2eakD的一般形态），未校准时行为不变，压缩后重放一致。
- `tests/context/test_engine.py`：校准的输入上限只放宽Planner限额。
- `tests/integrations/test_harnessix_configs.py`、`tests/product_config/test_server_and_cli.py`：评测配置不再覆盖
  压缩点；产品Server按稳定前缀与声明窗口接线。
