---
doc_type: adr
status: reviewing
version: 3
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
  - tests/processes/test_owner_standby.py
supersedes: []
---

# ADR 0168 长历史下每个模型步骤的宿主开销（O4a）

- 状态：评审中。产品负责人2026-11-17批准O4a：在不去掉、不减弱任何检查，不改变模型看到的内容（请求字节、
  持久事件、摘要与检查结论）的前提下降低每步宿主开销。本文的四项决策都满足这一前提；“需要决定的事项”一节
  列出超出这一前提、需要负责人另行决定的做法与测得的代价。第2版（2026-11-18）：负责人批准其中的D6（待命Owner），
  条件是在macOS、Linux、Windows三个平台正式验证，见“修订（第2版）”；D5、D7未批准，没有做
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
认证与Lease迁移不变，Owner进程与其能力摘要不变。原分支同一提交里的待命Owner第1版没有移植，第2版按负责人
决定移植（见“修订（第2版）”）。

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
数字是第121–160步、M3 Max上的估算（单项测量乘每步次数）；同一基准在Linux评测机上的宿主时间约为本机的两倍。

| 编号 | 做法 | 每步约省 | 为什么需要决定 |
|---|---|---|---|
| D1 | 写入方用刚归约出的Thread预填解析缓存，提交后的第一次读取不再解析 | 210毫秒 | 推翻ADR 0133决策1第3条：要依赖“序列化再解析等于原对象” |
| D2 | 一次认证读取只算一次投影SHA-256，同一摘要分别与签名检查点、行内摘要比对 | 40毫秒 | 现有测试明确要求两个校验器各算一次（`test_read_original_bytes.py`） |
| D3 | `calling_model`内的模型尝试事实（开始、用量、结束、响应终态）合并提交 | 每少一次提交约30毫秒，至多约200毫秒 | 改变崩溃恢复点（原ADR 0146草案已写明需要负责人决定） |
| D4 | `AgentRuntime._commit`不在追加前另读一次Thread，期望序号取自更轻的查询 | 60毫秒 | 追加事务内仍完整核对同一投影，但提交前那一次独立核对没有了；ADR 0133把它列为后续 |
| D5 | Session存储复用SQLite连接 | 约65毫秒（与历史无关） | 存储需要关闭生命周期与事务隔离保证，影响所有构造存储的调用方与Windows目录清理 |
| D6 | 待命Owner（原`feat/r2-cmdpath`第1项） | 每条命令约0.2秒（Linux实测进程启动215毫秒，原分支本机测得启动由115降到5.5毫秒） | 改变Owner进程的创建时机；原草案建议负责人单独确认。2026-11-18已批准，见“修订（第2版）” |
| D7 | POSIX Owner发布终态回执后以`os._exit`退出 | 每条命令约12毫秒（原分支本机测得；Linux上取回结果共35毫秒） | 跳过解释器收尾，需要逐条核实所有返回路径都已关闭并落盘（启动失败的路径不经`_close_all`） |
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

Linux（NL2Repo评测所用的同一台机器，Intel Xeon Gold 6330，112核，测量时平均负载约7；在宿主上直接运行，
不在任务容器内，不涉及Docker与GPU；墙钟时间的轮间波动在±1%以内）：

| 步骤 | `preparing_context` | `waiting_approval` | 模型调用内 | 宿主合计 | 工具执行 | CPU |
|---|---|---|---|---|---|---|
| 1–40 | 188 → 147 | 115 → 85 | 252 → 257 | 555 → 489 | 262 → 253 | 802 → 744 |
| 41–80 | 416 → 282 | 172 → 149 | 523 → 516 | 1110 → 947 | 361 → 364 | 1515 → 1362 |
| 81–120 | 632 → 413 | 226 → 206 | 781 → 786 | 1643 → 1401 | 462 → 455 | 2246 → 1993 |
| 121–160 | 866 → 540 | 304 → 273 | 1057 → 1052 | 2226 → 1872 | 589 → 579 | 2942 → 2579 |

三轮的全程每步宿主时间：改动前1413、1394、1384毫秒，改动后1188、1182、1191毫秒。
Linux上第121–160步的各阶段与NL2Repo实测在同一量级（`preparing_context` 0.87秒对评测平均0.8秒；尝试开始前0.20秒、
尾段0.12秒、尝试结束后0.36秒对评测0.23、0.18、0.47秒），基准复现了评测的形状。

同一台Linux机器上第121–160步逐函数计时（墙钟，含嵌套调用，与其他协程交错时会重叠，只用来看去向；毫秒/步）：

| 位置 | 每步次数 | 改动前 | 改动后 |
|---|---|---|---|
| 准备模型视图（三遍） | 3 | 228 | 84 |
| 选择工具（含交给视图的Turn） | 2 | 173 | 5 |
| Reducer归约（含核对`ModelHistoryPrepared`的视图） | 18.1 | 91 | 36 |
| 投影解析（含缓存命中） | 124 | 664 | 702 |
| 保存投影 | 15.3 | 486 | 488 |
| 认证读取投影 | 62 | 1753 | 1696 |
| 出站请求密钥扫描 | 1 | 60 | 60 |

命令路径（`scripts/command_path_bench.py`，40条简单只读命令，Linux同一台机器，交替各三轮，各轮中位数的中位数，毫秒）：

| 阶段 | 改动前 | 改动后 |
|---|---|---|
| 审批往返 | 62.5 | 46.7 |
| 恢复执行 | 100.2 | 85.4 |
| 进程启动（Owner解释器启动与导入为主） | 214.8 | 214.0 |
| 取回结果 | 35.2 | 34.0 |
| 宿主开销合计 | 537.0 | 505.6 |

- 第121–160步：`preparing_context`下降约38%（Linux 866→540毫秒，macOS 421→255毫秒），来自模型视图逐项复用与
  工具视图不再深拷贝；审批往返下降10%（Linux）到17%（macOS），来自长轮询唤醒；宿主合计下降16%（Linux）到22%
  （macOS）。模型调用内在Linux上没有变化（macOS上的约10%在噪声范围内），那里几乎全是Session提交与认证读取；
  工具执行（含进程路径）只降约2%：回执轮询加倍在Linux上每条命令只省1–2毫秒，进程启动与取回结果的时间主要是
  Owner解释器的启动与收尾（见D6、D7）。
- 每步宿主时间仍随历史增长，剩下的主要是“需要决定的事项”表中的提交、解析与认证读取。按Linux上的降幅估算，
  NL2Repo每题约161步、`preparing_context`平均0.8秒，约省每题40–55秒，即每100分钟的任务不到1分钟（估算，
  未在评测中测量）。

## 修订（第3版，2026-11-18）：D1–D4

### 负责人决定

产品负责人2026-11-18批准（“按推荐”）：D2、D4直接做；D1做，但要有往返一致的测试与运行中的抽样核对；D3只做一部分，
“尝试开始”仍单独、最先提交；D5、D7、D8不做（D6见第2版）。

### 做法与等价性

| 项 | 做法 | 没有改变的 | 证明 |
|---|---|---|---|
| D2 | 一次认证读取只算一次投影SHA-256，同一摘要分别与签名检查点、行内摘要比对（[`projection_identity`](../../src/harnessix/session/projection_identity.py)） | 两处比对、MAC与SQL核对、错误码 | `tests/session/test_read_original_bytes.py`改为钉住“算一次、比两处”，任一处不符仍以原错误失败 |
| D4 | `AgentRuntime._commit`拆成公开输出检查（`_protect_payloads`）与追加（`_append_payloads`）；追加前的期望序号取自投影行记录的序号（`SessionStore.committed_sequence`），没有投影行时改走`get_thread` | 追加事务内仍完整认证、解析并比对投影；损坏的投影仍以原错误码失败、什么也不写 | `tests/session/test_committed_sequence.py`：产品Turn的每次提交都与完整读取的序号比对，并钉住错误一致 |
| D1 | 追加提交后，存储把Reducer刚得到的Thread记作本次写入字节（按行摘要为键）的解析结果，提交后的第一次读取不再解析（推翻ADR 0133决策1第3条） | 每次读取仍重读行、重算摘要并做全部MAC、摘要与SQL核对，只省去解析 | 运行中抽样：每个缓存的第一次预填及其后每第16次（`PREFILL_CHECK_EVERY`）真的解析写入的字节，按类型、值、键序、浮点符号与时区对象严格比对（`projection_identity.identical`）；不符时不预填、丢弃该项、记录警告并停止预填，之后的读取重新解析。根`conftest`在整个测试集里核对每一次预填，任何不符都使测试失败；`tests/session/test_projection_prefill.py`核对评测形态产品运行的每次提交，并钉住不符时从不交出、回滚的追加从不预填 |
| D3 | `calling_model`内，成功尝试的完整用量与“尝试完成”在收到时照原样做公开输出检查，但留到与响应终态（`UsageRecorded`）同一事务提交（[`attempt_fact_commits`](../../src/harnessix/agent/attempt_fact_commits.py)）。收到其他Provider事件（正文、思考、下一次尝试、失败）、事件流结束或步骤以错误或取消离开时，先按原顺序把留存的事实提交 | “尝试开始”仍单独、最先提交，在向Provider要下一个事件之前落盘；部分用量、失败或取消的尝试结束仍立即提交；Reducer拒绝合并批次时逐个重做，得到与此前相同的持久结果与错误码 | `tests/agent/test_attempt_fact_commits.py`：六种情形（正文先于用量、OpenAI顺序、失败后重试、用量被Reducer拒绝、留存期间出错、流没有终态结束）分别合并与逐个提交，要求持久事件及其顺序、模型请求与结果都相同 |

**新的崩溃恢复点（D3）。** 成功尝试的完整用量之后崩溃时，用量与“尝试完成”要么一起落盘、要么都没有：此前可能只落下
用量。一起落盘时这次尝试按已完成恢复；都没有时与“只落下尝试开始”相同，按进行中的尝试结算（ADR 0144）。两处崩溃点
测试（`tests/agent/test_attempt_crash_recovery.py`、`tests/models/test_attempt_crash_recovery.py`）按此改写。

### 测得的数字

160步长历史基准（`scripts/host_step_overhead_bench.py --steps 160`），改动前（`b6509177`，已含本ADR第1版与O1、O2）
与改动后交替各跑三轮，毫秒/步：

| 机器 | 第121–160步宿主合计 | 其中模型调用内 | 全程每步 | CPU每步 |
|---|---|---|---|---|
| Linux（Intel Xeon Gold 6330，112核，开发服务器，负载约1） | 1852 → 1255（−32%） | 699 → 360 | 1166 → 833（−29%） | 1214 → 857 |
| macOS（Apple M3 Max，本机有其他会话负载） | 952 → 651（−32%） | 356 → 190 | 606 → 437（−28%） | 599 → 418 |

三轮之间的波动：Linux在±1%以内，macOS在±2%以内；每一轮都下降，工具失败为0。下降几乎都在模型调用内（D1、D3）与
准备上下文（D1、D2、D4）。Linux测量不在NL2Repo评测机上（那台机器在跑正式评测），评测里的实际收益待下一次评测核对。

## 考虑过的其他做法

- **把投影解析改成增量。** 投影是一整份JSON，局部变化后只解析变化的部分需要自写解析器，与原解析逐字节等价
  难以证明，未采用。
- **缓存出站请求的逐项扫描结果。** 扫描的节点与字节预算作用于整个请求，逐项复用要另行累计预算并处理跨项边界，
  每步只省27毫秒，未采用。
- **JSON往返代替深拷贝。** 只快约30%，且依赖往返相等，未采用。

## 回退

四项互不依赖。第1项见ADR 0155；第2项去掉`ThreadSignals`对`AppendSignal`的订阅；第3项去掉`WrapUpToolView`
的`reads_turn_in_place`；第4项把`receipt_poll_delays`换回常数。都不涉及数据库、协议、配置或Schema。
第2版的待命Owner单独回退：`action_runtime._process_supervisor`不再传`standby_owner=True`，产品即回到每条命令
现场启动Owner；同样不涉及数据库、协议、配置或Schema。

## 修订（第2版，2026-11-18）：待命Owner（D6）

### 负责人决定

产品负责人2026-11-18批准D6，条件是在macOS、Linux、Windows三个平台正式验证。D5（Session存储复用连接）与D7
（POSIX Owner以`os._exit`退出）不在批准范围内，没有做：Owner仍经解释器正常退出。

### 设计

1. **提前的只是Owner进程。** `PosixProcessSupervisor(standby_owner=True)`每次把Start帧交给Owner之后，在后台线程里
   为下一条命令启动一个Owner（[`owner_standby.py`](../../src/harnessix/processes/owner_standby.py)的`OwnerStandby`）。
   它以`--runs-directory <状态根>/runs`启动，阻塞在控制管道上；读到以换行结尾的完整Start帧后，按其中的Process ID
   定位运行目录，做与现场启动相同的检查（绝对路径、是目录、不是符号链接、属主为当前用户、组与其他用户无权限），
   然后才创建目标进程。“Lease `prepared`、运行目录、Lease `starting`、Start帧、目标”的顺序不变。
2. **待命Owner不带任何一条命令的配置。** argv（含沙箱前缀`sandbox-exec`/`bwrap`）、cwd、环境、凭据、终端与stdin
   模式、期限、输入输出预算与输出保护都只在Start帧里，Owner身份与令牌按命令生成。启动Owner本身用到的东西——
   解释器、状态根、固定的三项环境变量、新会话、只传入控制管道读端——对一个Supervisor是常量，与现场启动的
   Owner完全相同（两者共用`spawn_posix_owner`）。命令Profile、原生沙箱Profile或环境在两条命令之间变化，只改变
   下一份Start帧。
3. **一个待命Owner只用一次。** 下一条命令开始时先把它从待命位置取走、再等它启动完成，并发的两条命令只有一条
   拿到它；收到Start帧的Owner就是这条命令的Owner，随命令结束而退出，不回到待命位置。取走之后核对：后台启动
   成功、进程仍在运行、启动前测得的能力摘要（含Owner源码与解释器）等于Supervisor冻结的能力摘要。任何一项不满足，
   或者写Start帧时管道已断开，就终止并回收它，本条命令现场启动，并且这个Supervisor此后不再准备待命Owner，
   回到原来的行为。
4. **至多一个待命Owner。** Supervisor关闭时先终止并回收它（还在启动的先等它启动完）；取走后等待被取消的，
   启动完成即回收，不交给别的命令。控制管道写端只在宿主进程内、不可继承；宿主崩溃或持有者未关闭即被回收时，
   写端关闭，待命Owner读到EOF后以2退出，不创建任何东西。
5. **范围。** 只有产品运行时的Supervisor开启（[`action_runtime._process_supervisor`](../../src/harnessix/product_config/action_runtime.py)，
   POSIX）：命令Profile、原生命令Profile与借用它的Git进程宿主都经过它。直接构造的Supervisor（独立Git、评测
   检查Profile、测试）默认关闭；每次产品启动的第一条命令仍现场启动。没有新增配置字段、Schema或协议版本；
   `owner_standby.py`加入POSIX Owner能力摘要的模块清单。

### 安全论证

| 关注点 | 为什么成立 | 测试（`tests/processes/test_owner_standby.py`，另注明的除外） |
|---|---|---|
| 先有Lease与运行目录，再有目标 | 待命Owner读到完整Start帧之前不创建目标；运行目录在Start帧之后检查，检查不过不创建任何东西 | `test_standby_owner_checks_the_run_directory_before_creating_anything`、`test_standby_owner_runs_the_command_in_the_directory_named_by_the_start_frame` |
| 配置不在命令之间串用 | 命令的配置只在Start帧；换了工作区、环境、终端模式与期限的第二条命令只看到自己的，Owner身份与令牌各不相同 | `test_each_command_gets_only_its_own_configuration_from_a_standby_owner` |
| 一个待命Owner只用一次、至多一个 | 先取走再等待；用过的随命令退出 | `test_only_the_first_command_starts_an_owner_on_the_spot`、`test_concurrent_starts_never_share_one_standby_owner` |
| 待命Owner在使用前崩溃 | 取走时已退出：回收、现场启动、不再准备；存活检查之后、写完之前退出：写入得到EPIPE，换行是最后一个字节，它不可能读到完整的Start帧，改为现场启动不构成重放 | `test_a_standby_owner_that_died_before_use_is_never_used_again`、`test_broken_pipe_at_handoff_falls_back_without_sending_the_frame_twice` |
| 为另一份实现准备的待命Owner | 启动前测得的能力摘要与冻结的不同即丢弃；磁盘上的实现在命令开始时与冻结能力不同，照旧以`process_capability_mismatch`拒绝 | `test_a_standby_owner_for_another_owner_implementation_is_discarded` |
| 启动失败、资源不足 | 后台启动失败：本条现场启动，此后不再准备 | `test_a_failed_standby_spawn_falls_back_and_stops_preparing` |
| 交接时的其他异常 | 与现场启动同一规则：终止并回收用到的Owner，Lease记为`failed/launch_failed`，同一Process ID不重放 | `test_handoff_failure_other_than_a_gone_owner_settles_as_launch_failed` |
| 取消与关闭 | 待命Owner接手的命令照常取消、整组终止；关闭Supervisor停止运行中的命令并回收下一个待命Owner；被取消的取走不复用它 | `test_cancelling_a_command_on_a_standby_owner_stops_its_process_tree`、`test_closing_the_supervisor_stops_a_command_on_a_standby_owner_and_the_next_standby`、`test_a_cancelled_take_discards_the_standby_owner_instead_of_reusing_it`、`test_closing_waits_for_a_standby_owner_still_starting` |
| 宿主崩溃 | 宿主被强杀后待命Owner读到EOF自行退出 | `test_a_standby_owner_does_not_outlive_a_host_killed_without_cleanup`、`test_standby_owner_exits_by_itself_when_the_host_side_of_the_pipe_is_gone`、`test_an_unclosed_holder_releases_its_standby_owner` |
| 产品运行时 | 三条命令只有第一条现场启动Owner | `tests/benchmarks/test_command_path_bench.py` |
| Windows不变 | 每条命令现场启动，没有待命Owner | `tests/processes/test_windows_supervisor.py::test_windows_supervisor_starts_every_owner_on_the_spot`（Windows实机） |

待命Owner与现场启动的Owner都是宿主进程的子进程、各自处于新会话，终端的Ctrl+C不送到它们；两者继承同样的
umask与资源限制，产品不修改这两项。

### Windows

Windows保持每条命令现场启动Owner：`WindowsProcessSupervisor`没有这个选项，`windows_owner.py`不变，共用的交接
函数在Windows上只走现场启动一支。Windows Owner的控制句柄经`handle_list`临时继承，Job Object与ConPTY在Owner内
建立，控制台事件发给同一控制台上的所有进程；这些在没有Windows机器的条件下无法逐项验证，按“不能证明安全就
保持原行为”不做。正式验证核对的是这一点没有变化（核对清单见[Process Runtime模块设计](../modules/processes.md)
的D6增量小节）。

### 测得的效果

`scripts/command_path_bench.py`，40条简单只读命令，改动前（`b6509177`）与改动后交替各跑三轮，每格是三轮各自
中位数的中位数（毫秒/条）；“进程启动”是审计记为running到Owner报告目标启动。

Linux（开发服务器，Intel Xeon Gold 6330，112核，测量时平均负载约1–2；在宿主上直接运行，不在容器内；
三轮之间的波动在±1%以内）：

| 阶段 | 改动前 | 改动后 |
|---|---|---|
| 审批往返 | 36.8 | 36.1 |
| 恢复执行 | 61.8 | 62.0 |
| 进程启动 | 208.1 | 7.4 |
| 取回结果 | 29.2 | 30.9 |
| 发布结果 | 17.2 | 15.8 |
| 每条命令的宿主开销 | 402.5（三轮400.8、402.5、404.8） | 207.8（209.9、207.8、205.9） |
| 每步合计（含下一步的准备上下文） | 728.8 | 535.7 |
| 每步宿主进程CPU | 570.0 | 555.3 |

40条命令的整个Turn：改动前31.7、31.6、31.5秒，改动后23.9、23.9、23.9秒。

macOS（Apple M3 Max；测量期间本机有其他会话的负载，平均负载11–16，改动前第3轮受干扰明显）：

| 阶段 | 改动前 | 改动后 |
|---|---|---|
| 进程启动 | 144.4 | 8.2 |
| 取回结果 | 18.0 | 18.1 |
| 发布结果 | 14.8 | 19.6 |
| 每条命令的宿主开销 | 336.4（三轮336.4、323.2、480.6） | 182.2（188.6、179.5、182.2） |
| 每步合计 | 634.3 | 462.1 |
| 每步宿主进程CPU | 533.0 | 468.8 |

- 进程启动从约0.2秒降到几毫秒，剩下的是写Start帧与Owner发布第一份回执；每条命令的宿主开销下降约一半
  （Linux约195毫秒，macOS约155毫秒）。第一条命令仍现场启动，不在上表的中位数里显出来。
- 待命Owner的解释器启动与导入仍要花同样的CPU，只是挪到上一条命令运行期间、在另一个进程里；它与命令的取回、
  发布重叠，Linux上取回结果多1.7毫秒，macOS上发布多约5毫秒（负载下），都远小于省下的时间。每步宿主进程CPU
  不含Owner进程，Linux上基本不变；macOS上的差别在负载噪声之内。
- 每条命令仍是一个Owner进程，用过即退出；空闲时多一个阻塞在管道上的待命Owner进程（Linux上实测常驻内存33 MB，
  其中匿名内存19 MB，其余是与其他Python进程共享的文件页）。
