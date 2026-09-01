# Action 生命周期

## 1. 状态机

```text
RECEIVED
   ├── 校验失败 ───────────────────────────────► FAILED
   ▼
VALIDATED
   ▼
POLICY_EVALUATED
   ├── 策略拒绝 ───────────────────────────────► DENIED
   ├── 要求审批 ─► PENDING_APPROVAL
   │                  ├── 拒绝 ────────────────► DENIED
   │                  └── 批准
   ▼
READY
   ▼ 获取执行租约
LEASED
   ▼ 开始外部调用
RUNNING
   ├── 确定成功 ───────────────────────────────► SUCCEEDED
   ├── 确定未提交 ─────────────────────────────► FAILED
   └── 可能已提交 ─────────────────────────────► UNKNOWN
                                                    │
                                                    ▼
                                               RECONCILING
                                                  ├── SUCCEEDED
                                                  ├── FAILED
                                                  ├── UNKNOWN
                                                  └── MANUAL_INTERVENTION
```

## 2. `FAILED` 与 `UNKNOWN`

`FAILED` 表示执行器能够确认外部副作用没有提交，后续重试策略可以在满足条件时重试。

`UNKNOWN` 表示外部副作用可能已经提交。例如远端成功创建 Issue 后连接中断，本地没有收到响应。此时重新调用创建接口可能产生重复资源。

因此：

- `FAILED` 可以考虑重试；
- `UNKNOWN` 禁止盲目重试；
- `UNKNOWN` 应优先按业务幂等键或外部关联标识对账；
- 无法观察外部结果时进入人工介入，而不是伪造成功或失败。

## 3. 执行租约

`READY → LEASED` 表示 Worker 获得执行权，`LEASED → RUNNING` 表示外部调用已经开始。

`LEASED → RUNNING` 和 `RUNNING → 最终状态` 都校验租约 Owner 与截止时间。长任务由 Worker 周期性续租；续租只更新快照，不追加事件。失去租约的旧 Worker 即使稍后返回，也不能覆盖新状态。

租约过期恢复规则：

- `LEASED` 过期：外部调用尚未开始，可以回到 `READY`；
- `RUNNING` 过期：外部调用可能已经提交，进入 `UNKNOWN`；
- `RECONCILING` 过期：对账未得到确定结果，回到 `UNKNOWN`。

在 `queued` 模式中，`READY` 是持久队列状态，由独立 Worker 原子 Claim；在 `inline` 模式中，API 进程直接完成 Claim 和执行，便于本地调试。

## 4. 审批语义

审批记录包含请求指纹。Action 请求创建后不可修改，因此审批人看到的请求与执行器收到的请求保持一致，避免“批准 A、执行 B”的 TOCTOU 问题。

## 5. 幂等语义

幂等键在租户范围唯一。相同幂等键：

- 请求指纹一致：返回原 Action 快照；
- 请求指纹不一致：返回 `idempotency_conflict`；
- 不会创建第二个 Action，也不会重新执行原副作用。
