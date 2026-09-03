# ADR 0016：模型尝试账本与累计用量事实

- 日期：2026-09-03
- 状态：已接受；0.4.2b1 与 b2 已通过离线验收，SDK 映射细节见 ADR 0017
- 依据：ADR 0015；现有 UsageRecorded、纯 Reducer、SQLite 事件/投影原子提交

## 1. 为什么不能直接扩展成功响应的 Usage

一次 Model Step 可以有多个 HTTP 尝试。成功响应总量无法说明此前重试是否消费用量，也不能表示流中途失败、取消或进程中断。将缺失计数补成 0，会把“没有观测”误写成“没有消费”；把缓存/推理计数加到包含它们的总量上，又会重复计费。

本次不引入第二个数据库或独立账务服务，而是在现有事件日志中保存事实，用同一 Reducer 在线投影和离线重放。价格快照、币种、成本估算及账单核对留在 0.4.3。

## 2. 契约与提交顺序

Provider Event v2 在原事件之上增加三个可选元数据事件，与 Agent Event v4 的对应 Payload 共用供应商无关类型：

1. `ModelAttemptStarted`：内部 Attempt UUID、Step、从 1 连续递增的尝试序号、Provider、请求模型。
2. `ModelUsageObserved`：Attempt UUID、可选实际模型/响应 ID、累计用量完整快照。
3. `ModelAttemptFinished`：Attempt UUID、完成/失败/取消/中断结果、结构化错误。

Provider 必须先 yield Started，Kernel 提交后才继续下一次 anext，Adapter 此时才能发起 HTTP。Started 是**请求意图**，不能证明服务器收到了请求，也不能证明已计费。任意时刻同一 Turn 仅有一个开放尝试，Step 内至多 32 次；当前 SDK 配置仍至多 5 次。

元数据不是用户可见语义输出，不应锁死“首语义事件前重试”的 Adapter 门限。ResponseStarted 一旦出现，Kernel 禁止任何新尝试；成功尝试后不能重试。Finished 必须在 ResponseCompleted 前提交；只有最后一个成功尝试的完整总量与 ResponseCompleted 一致，才通过原有工具执行门禁。

响应身份一旦已知不可替换；后续观测省略身份表示保留。用量快照不可省略先前已知的计数，不允许回退。complete 之后可以补充原来未知的分项，不能修改已知终值。事件 sequence 决定因果顺序，不假定墙钟永远单调。

## 3. 数量包含关系与完整性

`UsageObservation` 的所有计数均为非负严格整数或 null，不接受 bool、数字字符串、浮点数。

- `input_tokens`：包含缓存读/写的输入总量。
- `output_tokens`：包含推理的输出总量。
- `uncached_input_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`：互斥输入分项；已知分项之和不得超过总量，全已知时必须等于总量。
- `reasoning_output_tokens`：输出子集，不得超过输出总量。
- unknown：全部计数未知；partial：有观测，但不是可信最终完整总量；complete：输入/输出权威终值均已知，供应商未提供的明细仍可为 null。

尝试失败可以拥有 complete 用量，成功尝试必须拥有 complete 用量和响应身份。计数完整不等于业务执行成功，也不等于供应商账单已核对。

## 4. 只有一份预算总量，防止双记账

保留 `Turn.usage` 的既有输入/输出整数 API，含义收敛为**已观测总量的累计下界**。每次观测只加同一 Attempt 新旧输入/输出之差；相同累计值重复到达不会重复增加。未提供输入总量时，不凭分项猜测完整总量，也暂不将该分项计入预算；原始分项仍保留在 Attempt 中。

每步仍保留 UsageRecorded，作为“响应完整结束”的既有门禁：

- 本步上报了 Attempt：校验最后一个成功尝试的总量，只推进 usage_step，不再次加总。
- 旧 Provider 未上报 Attempt：沿用原 UsageRecorded 加总路径。

这样混合旧/新 Provider 也不会产生两套相互矛盾的预算。后续重试/Model Step 会检查已知预算；无法从部分/未知用量保证真实消费不越界。

`Turn.usage_is_complete` 仅在所有已开始 Model Step 都有尝试账本、全部尝试已结束且用量 complete 时为 true。旧会话不凭空补出尝试、不把未知重试记成零；此属性为 false 不会阻止读取旧数据。该属性不是持久字段，也不是账单认证。

Token 指标在用量事实提交后按差额发送，包括失败前已提交的观测；指标不具备事件日志的 exactly-once 保证，进程在提交与指标发送之间崩溃时仍可能少报，应以日志重建值为准。

## 5. 失败、取消与恢复

流结束缺少完成事件、协议错误、取消、时间预算耗尽均由 Kernel 结算开放尝试。结算与 Error Item/Turn 终态在同一事务，不修改最后一条用量观测。没有观测就保持 unknown/null，不能宣称远端已取消或未收费。

Provider 在 CancelledError/GeneratorExit 清理期间不能额外 yield 收据；消费任务可能已经被取消，事件会丢失或触发异步生成器错误。宿主负责根据已经提交的事实结算。进程重启只标记 interrupted，不重放模型 HTTP 或工具。预算、存储或协议故障引起的失败与尝试元数据状态分别保留。

## 6. 版本与兼容

新增 `agent-event-v4`、`agent-thread-v4`、`provider-event-v2` Schema；旧文件冻结。新增 Migration 0004 推进最低读者版本，不改旧 SQL checksum，不重写旧事件或快照。首次新提交/重建使用 v4 投影。v1/v2/v3 事件不能包装新的尝试 Payload。

在修改领域模型前，从提交 `112471984d8aa6e23dae601cd4b3e1536f100fb5` 运行旧代码生成真实 v3 Transcript（包含两步工具闭环和非零用量），与既有 v1/v2 样本一起验证升级后旧事件字节不变、导出 JSON 不变、混合 Replay 一致。

## 7. 切片验收与剩余工作

0.4.2b1：领域约束、Reducer/预算、Provider v2 Kernel 消费、重复观测幂等、失败/取消/恢复、事务与运行时进程崩溃矩阵、旧会话升级、全量回归。

0.4.2b2 已完成离线验收：两个实际 SDK 发出上述元数据；核对锁定 SDK 与官方用量字段，映射缓存/推理子集及部分用量；覆盖 HTTP/SSE 失败、重试、取消、未知用量、跨 Provider 共享契约及独立可选安装。实现细节与边界见 [ADR 0017](0017-provider-attempt-usage.md)，仍不能声称真实平台兼容性已经验收。

后续 0.4.3a 已完成版本化价格与事后成本报告的离线验收，见 [ADR 0018](0018-versioned-token-cost.md)；真实 API 尚未验收。本切片不需要模型 Key 或远程中间件。
