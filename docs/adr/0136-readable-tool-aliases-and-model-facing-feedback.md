---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [models, agent, trusted_actions]
related_adrs:
  - docs/adr/0008-provider-event-model.md
  - docs/adr/0014-openai-compatible-provider.md
  - docs/adr/0015-anthropic-provider.md
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
related_tests:
  - tests/models/test_tool_alias_identity.py
  - tests/models/test_rejection_feedback.py
  - tests/models/test_current_tool_catalog.py
  - tests/agent/test_planning_tool.py
  - tests/trusted_actions/test_process_preview_projection.py
  - tests/product_ui/test_tool_summary.py
supersedes: []
---

# ADR 0136 可读工具别名、给模型的英文反馈与计划调用位置

- 状态：评审中
- 日期：2026-11-11
- 修订：[ADR 0014](0014-openai-compatible-provider.md)中“工具名以稳定哈希别名映射到函数名”一条（别名改为可读替换，
  摘要只在目录内冲突时作为8位后缀出现）

## 1. 背景

SWE-bench Multilingual一次正式运行（58个有效试次，模型qwen3.8-flash-next）里，下面几类问题都不改变模型的
决策，只让正确的意图失败或多花输出Token：

| 现象 | 数据 | 原因 |
|---|---|---|
| 调用未注册的工具 | 45次`unknown_tool`，多数下一步就是Shell调用 | `exec_command.testbed`含点号，别名成为`hx_exec_command_testbed_`加32位摘要，共56字符，模型抄错摘要 |
| Shell步骤输出偏多 | 1,358个Shell步骤，每步约多35个输出Token | 同上：每次调用都要生成32位十六进制 |
| 被拒后不知道该怎么办 | 结果只有“工具未注册” | 历史里模型自己的调用被换成`harnessix_rejected_tool_v1`和空参数，中文说明没有解释这一点 |
| `update_plan`被拒 | 28次，其中24次是“先`update_plan`、再一个其他调用” | 计划调用必须是响应的最后一个调用；其余4次是步骤缺`description` |
| 命令结果读不懂 | 每次非零退出都是中文说明；超时只有“Trusted Action未成功” | prometheus-15142（CVeydDJ第79步）测试命令挂到900秒上限被终止，同一命令里排在后面的恢复步骤没有执行，模型两步后才发现修改不在工作区 |

这些文字同时受一条界面规则约束：对话记录把工具失败显示为“错误码对应的固定中文”，没有映射的错误码才显示
`error.message`的第一句（[`tool_summary.py`](../../src/harnessix/product_ui/tool_summary.py)）。

## 2. 决策

### 2.1 工具别名只替换非法字符

[`_history.py`](../../src/harnessix/models/_history.py)：

- `tool_alias(name)`：`[A-Za-z0-9_-]`以外的字符逐个换成下划线，截到64字符；合法原名不变。
  `exec_command.testbed`→`exec_command_testbed`。
- `tool_aliases(names)`：一份目录的原名到别名映射。可读别名互不相同时直接使用；多个原名替换后相同时，
  本身就是该别名的合法原名保留它，其余改为“可读片段前55字符 + `_` + SHA-256(原名UTF-8)前8位”。
  `{a.b, a_b}`→`a_b_<8位>`与`a_b`。
- 别名等于历史拒绝哨兵`harnessix_rejected_tool_v1`的工具恒加后缀，哨兵不被广告。

保持的不变量：

1. **一一对应。** 一次请求内别名互不相同；加后缀后仍相同（后缀别名恰好是另一个已注册原名）或原名重复时，
   请求在发送前整体拒绝（`wire_directory`，原`InvalidModelRequest`）。
2. **确定。** 映射只由目录内的名字集合决定，与顺序、进程、时间无关；摘要基于原名精确UTF-8字节。
3. **响应只匹配本次反向目录。** 不接受注册原名、大小写变体、旧摘要别名或哨兵；这些仍成为目录拒绝。
4. **会话不保存别名。** `ToolCallContent.tool`一直是注册原名，别名在每次请求映射时重新计算，
   所以旧版本写入的会话在新版本下直接以可读别名重放，不需要迁移。

消费方：两个Provider映射的工具定义与反向目录（`wire_directory`）、强制工具选择、目录说明中的强制别名
（`wire_aliases`）、历史重放（`_history_alias`）。历史里不在本次目录的旧工具用可读别名；若与本次目录的
某个别名相同则加后缀，旧调用不会看似指向当前工具。

### 2.2 目录拒绝：持久结果不变，模型看到英文反馈

目录拒绝的持久结果是每个原因一份的固定文本（`unknown_tool`/“工具未注册”等），追加、重放、Fork和界面都
按逐字相等校验或显示它，不能改。给模型的文字改在模型线协议视图里替换：`messages_for`对已配对拒绝的结果，
把`error.message`换成[`rejection_feedback`](../../src/harnessix/agent/tool_rejections.py)的英文说明，
其余字段与持久结果相同。

- 说明内容：调用没有执行；原名称与参数不保留在历史里；用本次请求提供的工具的准确名字重发完整调用。
- 说明只由拒绝原因决定，不含调用名称、参数或目录内容，所以历史正文不随目录变化，前缀缓存与输入估算
  的前缀证明不受影响。可调用的名字由同一请求的工具定义和目录说明（`Current request tool catalog`）给出。

### 2.3 `update_plan`可以写在响应的任意位置

保留的不变量：计划Item只能在整组调用结算后、进入准备阶段时，与它的结果在同一事务里发布（Reducer只在
`PREPARING_CONTEXT`接受计划，结果必须按调用顺序结算）。这保证“成功结果与计划同时存在”，崩溃恢复不会看到
只有一半的计划更新。

做法：模型响应形成持久调用组时，`plan_calls_last`把计划调用稳定地移到其余调用之后
（[`planning.py`](../../src/harnessix/agent/planning.py)，`AgentRuntime._sample_events`调用）。计划只是自报
记录，不读写工作区，后移不改变其他调用的输入、顺序和效果。事件、Reducer和计划结果合同不变。

- 同一响应里有多个计划调用时，只有最后一个被记录；较早的以`tool_invalid_arguments`失败并说明原因。
- 未启用内置规划时，同名自定义工具保持模型给出的位置。
- 模型历史按持久顺序重放，计划调用显示在该响应调用组的末尾。

参数校验规则不变，错误说明改为英文；形状与字段错误仍附一份完整合法调用。工具说明改为英文并写明
“可与其他调用同处一个响应，位置不限”，因此`update_plan`的工具指纹变化。

### 2.4 命令结果

[`agent_gateway_output._process_failure_feedback`](../../src/harnessix/trusted_actions/agent_gateway_output.py)：

- `process_nonzero_exit`：英文说明“命令已运行并以非零状态退出，不是启动失败”，以及预览是否已是全部输出。
- `process_timeout`：英文说明命令到达时限被终止、没有跑完；复合命令里尚未开始的部分没有执行；
  继续之前先检查命令留下的状态。只陈述这一通用事实，不识别具体命令。

两个错误码在界面都有固定文案（“命令以非零状态退出”“命令超时”），界面不读这段文字。

### 2.5 模型文案与界面文案的分界

| 文字 | 模型看到 | 用户看到 | 分界 |
|---|---|---|---|
| 目录拒绝结果 | 英文反馈（视图替换） | 持久固定中文 | 持久事实与模型视图分离 |
| `update_plan`校验错误 | 英文 | “参数无效，Agent 会调整后重试” | 界面按错误码映射 |
| 命令非零退出、超时 | 英文 | “命令以非零状态退出”“命令超时” | 界面按错误码映射 |
| 其他Trusted Action失败的通用说明 | 中文（未改） | 该中文的第一句 | 没有错误码映射，改动会改变界面 |

## 3. 未采纳的方案

- **响应接受注册原名或旧摘要别名。** 会话不保存别名，旧会话不需要它；它会给反向目录增加第二套名字，
  与“响应只匹配本次广告的精确别名”的既有边界冲突。
- **在拒绝结果里列出可调用工具名，或指出最接近的名字。** 列名字会让历史正文随目录变化，破坏
  “目录变化只改变反馈与广告”的既有不变量和前缀缓存。指出最接近的名字需要模型写出的名称，而
  `ToolCallRejected`与`ToolCallRejectionContent`按合同不携带名称，持久结果又是固定文本；实现它需要给
  Provider Event与Agent Event各加一个宿主派生字段并升级版本，属于公共合同变更，不在本决策范围。
- **放宽Reducer，让计划在调用组中间发布。** 改变事件流的合法形状，旧版本读不了新会话，并失去
  “结果与计划同事务”的保证；调整调用组顺序即可达到同样效果。

## 4. 后果

- 工具定义与历史中的工具名变短：线上字节变化一次（升级后的首个请求前缀缓存未命中）。
- 评测或日志里按`hx_`前缀识别工具的脚本需要改为按可读别名或注册原名识别。
- 升级前的会话里，旧`update_plan`调用的指纹与新说明不同：在模型再次更新计划之前，压缩不再固定那一组
  旧计划调用；计划Item本身不受影响。
- 被拒调用的原名称仍不进入事件流（见第3节第二项）。

## 5. 验证

- `tests/models/test_tool_alias_identity.py`：替换规则、冲突后缀、目录一一对应、强制选择与历史共用同一映射、
  旧会话重放、真实SDK流只接受本次精确别名（含旧摘要别名与注册原名的拒绝）。
- `tests/models/test_current_tool_catalog.py`：首个请求的线上字节与旧版本只差工具别名。
- `tests/models/test_rejection_feedback.py`：两种拒绝原因的英文反馈、持久结果不变、反馈不随目录变化、
  Runtime中“被拒后下一请求”的完整形态。
- `tests/agent/test_planning_tool.py`：计划调用在任意位置、多个计划调用、未启用规划时不重排、英文错误与示例。
- `tests/trusted_actions/test_process_preview_projection.py`：非零退出与超时的英文说明。
- `tests/product_ui/test_tool_summary.py`：上述错误码在界面的文案不变。
