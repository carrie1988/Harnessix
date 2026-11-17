---
doc_type: change-design
status: current
version: 1
code_revision: 9c6c60ac3954edbe8943493b8b1c07d313c807aa
owners:
  - core
modules:
  - agent
  - context
  - app_server
  - session
  - processes
  - product_config
related_adrs:
  - docs/adr/0168-host-overhead-per-model-step.md
  - docs/adr/0155-model-view-item-reuse.md
  - docs/adr/0133-parsed-projection-reuse-and-reducer-sharing.md
related_tests:
  - tests/benchmarks/test_long_history_overhead_bench.py
  - tests/benchmarks/test_host_step_overhead_bench.py
  - tests/benchmarks/test_command_path_bench.py
  - tests/context/test_model_view_memo.py
  - tests/agent/test_tool_view_turn.py
  - tests/app_server/test_events_wake.py
  - tests/session/test_append_signal.py
  - tests/processes/test_receipt_polling.py
supersedes: []
---

# 长历史下每个模型步骤的宿主开销（O4a）

## 1. 文档摘要

产品负责人2026-11-17批准的O4a：不去掉、不减弱任何检查，不改变模型看到的内容，降低每个模型步骤在模型之外的
宿主时间。本次做四件事：移植模型视图的逐项复用（ADR 0155）、Turn等客户端时立即唤醒长轮询、声明只读的工具视图
不再拿深拷贝、Owner回执轮询从1毫秒起加倍；另加一个NL2Repo规模的本地基准。决策、测得的结果与需要负责人决定的
其余做法见[ADR 0168](../adr/0168-host-overhead-per-model-step.md)。

## 2. 需求背景

NL2Repo正式评测（Linux，0.10.0b48）每步宿主时间：`preparing_context`平均0.8秒，自动审批往返0.43秒，
`calling_model`内模型之外约0.9秒，合计每100分钟的任务约5分钟，且随历史增长。此前的本地基准只到60步、结果
1–3 KB，复现不了13万Token历史下的形状。

## 3. 设计目标与非目标

- 目标：第121–160步每步宿主时间可测地下降；请求字节、持久事件、摘要与检查结论逐字节不变；有本地基准能复现与
  验证。
- 非目标：改变Session提交次数或崩溃恢复点、去掉重复的核对、改变协议客户端的轮询方式、待命Owner（都列在ADR 0168
  “需要决定的事项”）。

## 4. 总体架构

| 层 | 变化 |
|---|---|
| Context（`context/model_view_memo.py`、`tool_result_view.py`等） | 模型视图逐项结果按严格内容指纹复用，只用来比对的视图不拷贝（ADR 0155） |
| Agent（`agent/request_notes.py`、`ports.py`，`runtime.py`改一行） | `tool_view_turn`：声明`reads_turn_in_place`为`True`的视图拿浅副本 |
| Product Config（`product_config/wrap_up.py`） | 没有内层视图的`WrapUpToolView`声明只读 |
| Session（`session/append_signal.py`、`sqlite.py`、`ports.py`） | 追加提交后的进程内通知 |
| App Server（`app_server/query_runtime.py`、`service.py`） | Turn等客户端或结束时唤醒`events/next` |
| Processes（`processes/receipt_polling.py`、`supervisor.py`） | 回执轮询间隔从1毫秒起加倍 |
| 基准（`scripts/long_history_overhead_bench.py`等） | 160步NL2Repo规模的脚本化Turn，按Session事件切分阶段 |

## 5. 接口设计

- `agent.ports.InPlaceTurnToolView`：在`ModelToolView`之上多一个只读属性`reads_turn_in_place`。Runtime只认布尔
  `True`；没有该属性或为其他值时照旧深拷贝。
- `agent.request_notes.without_request_notes(turn, *, detached=True)`与`tool_view_turn(view, turn)`。
- `session.ports.SessionStore.appends: AppendSignal`；`AppendSignal.subscribe`返回取消订阅函数，`notify`吞掉订阅者
  异常。
- `processes.receipt_polling.receipt_poll_delays(ceiling, *, limit=None)`。
- 协议、事件、配置与Schema不变。

## 6. 数据结构与领域契约

不新增持久数据结构。模型视图复用表与长轮询信号都只在进程内；视图对象、Thread对象不进表。

## 7. 流程与数据流

1. `preparing_context`：模型视图三遍准备里，未变化的历史Item查表得到核对结论与规范JSON（ADR 0155）；目录说明与
   请求各问一次工具视图，`WrapUpToolView`拿浅副本。
2. 自动审批：审批请求提交→`AppendSignal`→Turn处于`waiting_approval`→置位长轮询信号→客户端立即读到审批请求。
3. 命令执行：Owner回执先按1、2、4、8毫秒轮询，简单命令在几毫秒内拿到终态。

## 8. 失败、恢复、取消与超时

- 复用表只记成功结果；内容变化必定重新核对并得到原来的错误。
- 通知丢失（进程内故障、不经`append`/`fork`的写入）只让读者晚一个50毫秒周期；订阅者异常不影响已提交的追加。
- 启动结果等待仍是500次轮询（约5秒）后关闭并以`process_launch_failed`结束；取消路径不变。

## 9. 持久化、事务与数据流程

Session、Lease、审计与执行计划的持久化内容、顺序与事务边界都不变。

## 10. 安全、权限与信任边界

- 每次读取的摘要、MAC与SQL核对，提交前的公开输出检查，出站请求的密钥扫描，回执认证都不变。
- 工具视图的共享面与同一视图的`admit_call`相同；没有声明只读的视图仍拿独立副本。ADR 0133的“共享对象不得原地
  修改”约束适用。

## 11. 可观测与错误分类

不新增错误码或遥测字段。基准从Session事件的时间戳切分阶段，不给产品代码加探针。

## 12. 测试与验收

- 等价性：产品运行中每次准备模型视图与每次选择工具都与从头计算的结果比对；内容变化时仍重新核对。
- 行为：长轮询唤醒与追加通知的既有回归随移植带入；回执轮询序列与次数上限。
- 基准：长历史基准的9步冒烟在产品运行时上跑通。
- 全量：分支与`9c6c60ac`全量测试的失败集合逐项对比（Homebrew Git），结果见ADR 0168“后果”。

## 13. 源码与测试映射

| 源码 | 测试 |
|---|---|
| [`context/model_view_memo.py`](../../src/harnessix/context/model_view_memo.py)、[`context/tool_result_view.py`](../../src/harnessix/context/tool_result_view.py) | `tests/context/test_model_view_memo.py`、`tests/benchmarks/test_host_step_overhead_bench.py` |
| [`agent/request_notes.py`](../../src/harnessix/agent/request_notes.py)、[`agent/ports.py`](../../src/harnessix/agent/ports.py)、[`product_config/wrap_up.py`](../../src/harnessix/product_config/wrap_up.py) | `tests/agent/test_tool_view_turn.py` |
| [`session/append_signal.py`](../../src/harnessix/session/append_signal.py)、[`app_server/query_runtime.py`](../../src/harnessix/app_server/query_runtime.py) | `tests/session/test_append_signal.py`、`tests/app_server/test_events_wake.py` |
| [`processes/receipt_polling.py`](../../src/harnessix/processes/receipt_polling.py) | `tests/processes/test_receipt_polling.py` |
| [`scripts/long_history_overhead_bench.py`](../../scripts/long_history_overhead_bench.py) | `tests/benchmarks/test_long_history_overhead_bench.py` |

## 14. 部署、兼容与回退

不涉及数据库、协议、配置或Schema，三个平台行为相同（回执轮询的启动等待只在POSIX Supervisor上）。四项各自回退，
见ADR 0168“回退”。

## 15. 风险与取舍

- 每步宿主时间仍随历史增长：主要剩在每次提交后的投影解析与序列化、每次认证读取的取回与核对，以及每步约15次提交；
  继续下降需要负责人在ADR 0168列出的D1–D8中取舍。
- 本地基准的工具结果是确定的小文件与命令输出，与真实任务的分布不同；绝对值以评测机为准，本地只用于前后对比。
