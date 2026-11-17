---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, context, app_server, session, processes, product_config]
related_adrs:
  - docs/adr/0126-incremental-session-history-verification.md
  - docs/adr/0133-parsed-projection-reuse-and-reducer-sharing.md
  - docs/adr/0155-model-view-item-reuse.md
  - docs/adr/0072-durable-interaction-and-pull-live-stream.md
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

# ADR 0168 长历史下每个模型步骤的宿主开销（O4a）

- 状态：评审中。产品负责人2026-11-17批准O4a：在不去掉、不减弱任何检查，不改变模型看到的内容（请求字节、
  持久事件、摘要与检查结论）的前提下降低每步宿主开销。本文的四项决策都满足这一前提；“需要决定的事项”一节
  列出超出这一前提、需要负责人另行决定的做法与测得的代价
- 日期：2026-11-16
- 编号说明：原`feat/r2-cmdpath`分支上的“ADR 0146 命令路径的宿主开销”与主线ADR 0146（思考强度档位）重号，
  其中移植过来的部分归入本文；模型视图的逐项复用仍是[ADR 0155](0155-model-view-item-reuse.md)

## 背景

NL2Repo正式评测（Linux，0.10.0b48，39题，每题约161次模型调用，历史最长约13万真实Token）里，每个模型步骤
在模型之外的宿主时间：

| 部分 | 每步 |
|---|---|
| `preparing_context`（不含压缩） | 平均0.8秒（6,278步合计83分钟） |
| `waiting_approval`（Harbor客户端自动审批） | 平均0.43秒 |
| `calling_model`内：状态变为`calling_model`到`model_attempt_started` | 中位数0.23秒 |
| `calling_model`内：最后一次用量到尝试结束 | 0.18秒 |
| `calling_model`内：尝试结束到下一次状态变化 | 0.47秒 |

合计约每100分钟的任务5分钟；mini-swe-agent这部分接近零。各部分都随历史增长。

### 本地复现

`scripts/long_history_overhead_bench.py`以评测同款装配（产品配置、外部边界命令Profile、协议客户端自动审批）
跑160步的脚本化Turn：40 KB说明作为任务，之后循环创建模块与测试、读回、编辑、列目录、运行命令，历史每步约
增长4 KB；脚本模型按实际收到的历史上报输入Token（每Token 3.6字节），校准后的压缩点落在真实窗口一半附近，
160步内压缩一次（与评测每题一到两次相当）。阶段取自Session事件的时间戳，与评测诊断同一口径；有压缩的步骤
单独计数、不计入各桶平均。

第121–160步（历史约360–440 KB估算字节，Thread投影1.78 MB，会话共2,908个事件）逐处计时与单项测量
（Apple M3 Max；单项代价是对第160步状态的独立测量，每步次数来自逐函数计时，乘积是估算）：

| 位置 | 每步次数 | 单次 | 每步约 | 随历史增长的原因 |
|---|---|---|---|---|
| Session提交 | 15.3 | — | — | 见下面三行；其中8次落在`calling_model`内（尝试开始、用量、尝试结束、正文与调用Item等各自一次） |
| 提交后下一次读取解析新投影 | 15.3 | 13.6毫秒 | 210毫秒 | 投影是整个Thread（Item、每步的Context检查记录等）；ADR 0133规定写入方不预填解析缓存 |
| 保存投影：整份序列化 | 15.3 | 6.6毫秒 | 100毫秒 | 持久字节就是整份投影 |
| 认证读取投影（运行时、提交事务内与客户端） | 61 | 约4.5毫秒 | 270毫秒 | 每次读取：取回1.78 MB原字节（1.2毫秒）、两次SHA-256（各0.64毫秒）、按事件数计数的联表核对（1.0毫秒）、新开SQLite连接（0.67毫秒）等 |
| 其中客户端`thread/get` | 16 | 同上 | 约70毫秒 | 协议客户端每取一页事件就读一次Thread |
| 模型视图（每步三遍） | 3 | 34毫秒 | 102毫秒 | 每遍从头处理全部历史Item（ADR 0155） |
| 交给工具视图前深拷贝整个Turn | 2 | 31毫秒 | 63毫秒 | 本决策第3项 |
| 出站请求密钥扫描 | 1 | 27毫秒 | 27毫秒 | 扫描整个请求；节点与字节预算作用于整份请求 |
| 历史Artifact引用核对 | 1 | 25毫秒 | 25毫秒 | ADR 0158之后只核对记录与摘要 |

审批往返另有一项与历史无关的等待：`events/next`只被正文增量唤醒，审批请求要等下一次50毫秒定时重读。
命令路径上，Owner回执按10毫秒（启动）与20毫秒（运行）的固定间隔轮询。

## 决策

### 1. 模型视图逐项复用（ADR 0155，自`feat/r2-ctxprep`移植）

决策与证据见ADR 0155，代码不变。移植时唯一的冲突是主线在`_model_view`旁新增的`plan_history_compaction`。

### 2. Turn停下来等客户端或已经结束时，`events/next`立即返回（自`feat/r2-cmdpath`移植）

`SQLiteSessionStore.appends`（`session/append_signal.py`）在追加事务提交之后、故障注入点之后把追加后的Thread
告知进程内订阅者；App Server只在活动Turn处于`waiting_approval`、`waiting_input`或Thread已没有活动Turn时置位该
Thread的长轮询信号。

1. 通知只是“现在重读能看到这次追加”的提示。被唤醒的读取仍从存储取数并过公开输出检查；50毫秒定时重读保留，
   漏发只让读者晚一个周期。
2. 不在每次提交时唤醒：试过，客户端会为每个事件多发一轮请求，与Turn争用事件循环，省下的等待被抵掉。

### 3. 声明只读的工具视图拿Session中的Turn，不先深拷贝

每个模型步骤问两次工具视图本步提供哪些工具（目录说明与请求本身），此前每次先深拷贝整个Turn。
`ModelToolView`可以声明`reads_turn_in_place`（`agent/ports.py::InPlaceTurnToolView`）：

1. 只有该属性恰为`True`的视图拿到去掉请求说明、共享其余冻结对象的浅副本；其余视图（包括第三方视图、
   有内层编码阶段视图的`WrapUpToolView`）照旧拿到独立的深拷贝。
2. 产品里通用执行时安装的`WrapUpToolView`（没有内层视图）声明为`True`：它的`select`只读本轮用户消息的正文。
3. 同一个视图的`admit_call`、工具步骤说明来源的`step_notice`本来就拿共享的Turn与Thread；ADR 0133已规定
   共享对象不得原地修改。这里与它们一致，没有引入新的共享面。

### 4. Owner回执的轮询从1毫秒起加倍

`SupervisedProcess.wait`与POSIX启动后等待启动结果的轮询，间隔改为1、2、4、8……毫秒，到原来的20毫秒与
10毫秒为止（`processes/receipt_polling.py`）。启动结果仍最多轮询500次（约4.975秒，原为5秒）。回执的读取、
认证与Lease迁移不变，Owner进程与其能力摘要不变。原分支同一提交里的待命Owner没有移植（见“需要决定的事项”）。

## 等价性与检查

- **请求字节、持久事件与摘要。** 第1项：产品运行中每次准备模型视图都用同一输入再从头逐项重算一遍并逐字节比对，
  每个出站请求的历史都等于某次从头重算的结果（`tests/benchmarks/test_host_step_overhead_bench.py`）；内容有任何
  变化都重新核对并得到原来的错误（`tests/context/test_model_view_memo.py`）。第3项：产品运行中每次选择工具都
  再用深拷贝的Turn选一遍并比对，结果相同；收尾视图在有无收尾说明时两条路径的选择相同，Session中的Turn序列化
  前后逐字节不变（`tests/agent/test_tool_view_turn.py`）。第2、4项不改变任何持久内容，只改变读取方被唤醒、
  轮询回执的时刻。
- **检查。** 每次读取的摘要、MAC与SQL核对，每次提交前的公开输出检查，每个出站请求的密钥扫描，模型视图对冻结
  决定的核对，回执的认证，都与原来逐项相同。

## 需要决定的事项

下面的做法每一项都能再省下可观的时间，但都会去掉一次重复的核对、改变已写明的规则或崩溃恢复点，本次没有做。
数字是第121–160步、M3 Max上的估算（单项测量乘每步次数）；Linux评测机上纯CPU阶段约为本机的两倍多。

| 编号 | 做法 | 每步约省 | 为什么需要决定 |
|---|---|---|---|
| D1 | 写入方用刚归约出的Thread预填解析缓存，提交后的第一次读取不再解析 | 210毫秒 | 推翻ADR 0133决策1第3条：要依赖“序列化再解析等于原对象” |
| D2 | 一次认证读取只算一次投影SHA-256，同一摘要分别与签名检查点、行内摘要比对 | 40毫秒 | 现有测试明确要求两个校验器各算一次（`test_read_original_bytes.py`） |
| D3 | `calling_model`内的模型尝试事实（开始、用量、结束、响应终态）合并提交 | 每少一次提交约30毫秒，至多约200毫秒 | 改变崩溃恢复点（原ADR 0146草案已写明需要负责人决定） |
| D4 | `AgentRuntime._commit`不在追加前另读一次Thread，期望序号取自更轻的查询 | 60毫秒 | 追加事务内仍完整核对同一投影，但提交前那一次独立核对没有了；ADR 0133把它列为后续 |
| D5 | Session存储复用SQLite连接 | 约65毫秒（与历史无关） | 存储需要关闭生命周期与事务隔离保证，影响所有构造存储的调用方与Windows目录清理 |
| D6 | 待命Owner（原`feat/r2-cmdpath`第1项） | 每条命令约0.1秒（本机），评测机估计0.28秒 | 改变Owner进程的创建时机；原草案建议负责人单独确认 |
| D7 | POSIX Owner发布终态回执后以`os._exit`退出 | 每条命令约12毫秒（本机） | 跳过解释器收尾，需要逐条核实所有返回路径都已关闭并落盘 |
| D8 | 编码阶段视图（`CodingWorkflowToolView`，非通用执行）也声明只读 | 63毫秒（只在该装配下） | 需要逐个审计约1000行的选择逻辑不修改传入的Turn |

另外，协议客户端（Harbor运行器，以及产品自己的`exec`与终端界面）每取一页事件就调用一次`thread/get`，每次都是
一次完整的认证读取；这属于客户端行为，本次不改，评测客户端更不能为测量而改。

## 后果

长历史基准，160步，改动前（`9c6c60ac`加基准脚本）与改动后交替各跑三轮，每格是三轮各自桶内平均的中位数
（毫秒/步；“模型调用内”是前段、流式记录、尾段与后段之和；CPU是本步进程CPU时间，含进程内的协议客户端）。

macOS（Apple M3 Max；测量期间本机有其他会话的负载，平均负载12–14，墙钟时间的轮间波动约±10%，CPU时间更稳）：

| 步骤 | `preparing_context` | `waiting_approval` | 模型调用内 | 宿主合计 | 工具执行 | CPU |
|---|---|---|---|---|---|---|
| 1–40 | 88 → 68 | 71 → 49 | 147 → 137 | 306 → 254 | 139 → 131 | 401 → 356 |
| 41–80 | 203 → 132 | 94 → 77 | 271 → 254 | 568 → 463 | 185 → 174 | 732 → 626 |
| 81–120 | 311 → 196 | 127 → 100 | 416 → 377 | 854 → 672 | 241 → 217 | 1129 → 915 |
| 121–160 | 421 → 255 | 154 → 128 | 565 → 505 | 1140 → 888 | 294 → 267 | 1473 → 1191 |

三轮的全程每步宿主时间：改动前737、729、630毫秒，改动后633、574、512毫秒，每一轮都下降。

<!-- Linux结果在测量完成后填写 -->

- 第121–160步宿主合计下降约22%，其中`preparing_context`下降约39%（模型视图与工具视图的深拷贝），审批往返下降
  约17%；模型调用内只降约10%，那里几乎全是Session提交与读取。
- 每步宿主时间仍随历史增长：剩下的主要是“需要决定的事项”表中的提交、解析与认证读取。按评测机的比例粗估，
  这次在NL2Repo上每100分钟的任务约省1分钟（估算，未在评测中测量）。

## 考虑过的其他做法

- **把投影解析改成增量。** 投影是一整份JSON，局部变化后只解析变化的部分需要自写解析器，与原解析逐字节等价
  难以证明，未采用。
- **缓存出站请求的逐项扫描结果。** 扫描的节点与字节预算作用于整个请求，逐项复用要另行累计预算并处理跨项边界，
  每步只省27毫秒，未采用。
- **JSON往返代替深拷贝。** 只快约30%，且依赖往返相等，未采用。

## 回退

四项互不依赖。第1项见ADR 0155；第2项去掉`ThreadSignals`对`AppendSignal`的订阅；第3项去掉`WrapUpToolView`
的`reads_turn_in_place`；第4项把`receipt_poll_delays`换回常数。都不涉及数据库、协议、配置或Schema。
