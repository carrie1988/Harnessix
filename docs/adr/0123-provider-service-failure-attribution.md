---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, evals]
related_adrs:
  - docs/adr/0093-kernel-owned-public-failure-contract.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/agent/test_model_attempts.py
  - tests/integrations/test_harnessix_eval.py
supersedes: []
---

# ADR 0123 服务方故障的如实归因

## 1. 背景

- **计费安全规则：** 产品在一次模型请求未完整报回用量时，不再发起新的付费请求。这条规则有三个执行点：
  - **步骤内重试：** 失败尝试按保守预留计入预算，下一次尝试因此超限；
  - **下一步骤开始：** 发现已有用量未完整的尝试，直接停止；
  - **请求额度检查：** 开启上下文压缩时，每次发起主模型或摘要请求前核对请求总数，同样拒绝用量未知（含摘要请求）之后的新请求。
- **问题：** 三处都以`budget_exceeded`结束。服务方临时故障（5xx、429、连接与超时）因此表现为“预算耗尽”，评测平台把它记为系统失败。矩阵与E0c都观察到了这一点（ADR 0120 §7.3）。

## 2. 用户决策（2026-11-07）

停止规则不变，原因如实标注；评测把服务方故障记为基础设施失败，可以重跑。

## 3. 决策

### 3.1 服务方故障码

模型适配器标为可重试的服务端失败：
- `provider_provider_internal`（5xx）
- `provider_rate_limit`（429）
- `provider_transport`（连接、超时、408、409）

集合与归因逻辑定义在`harnessix.agent.provider_stop`，各停止点共用。

### 3.2 三个停止点

- **下一步骤开始与请求额度检查：** 若用量未完整的已结束尝试（含摘要请求）中，有因上述故障失败的尝试，Turn以该尝试的原故障码结束（类别`provider`），否则仍为`budget_exceeded`。
- **步骤内重试：** 只在不计失败尝试的保守预留时预算足够的情况下，才归因于该服务方故障；否则仍是真实的预算不足，报`budget_exceeded`。
- **不变的部分：** 停止规则本身、预算计算和已完成用量的记账都不变。

### 3.3 评测归因

- **服务方故障：** Harbor解析器把类别为`provider`且错误码属于上述集合的试次，记为`infra_error`（`provider_unavailable`）。这类试次可以重跑，不计入主指标。
- **其他Provider失败：** 鉴权、请求无效、内容策略、上下文溢出、模型输出无效、额度，仍记`system_error`（`provider_failed`）。
- **代码同步：** 解析器在不安装产品的Harbor环境中运行，因此复制同一集合，由测试核对两处一致。
- **矩阵：** `05-protocol-provider-fail`的预期改为`infra_error / provider_unavailable`，原生错误码为`provider_provider_internal`。

## 4. 取舍

- **不放宽停止规则。** 一次已被重试成功的503，仍会在下一步骤开始时结束Turn。主流智能体会自动退避重试，但对“明确未计费的失败”放宽规则需要单独论证，留待以后。
- **只归因不重算。** 已完成的运行（如E0c）不追溯改写，新的运行按本ADR归因。

## 5. 验收

- **产品：** 以下五种情形都有测试覆盖：
  - 步骤内重试被拒、失败用量未知时，报原故障码；
  - 开启上下文压缩时，同一情形由请求额度检查拦截，也报原故障码（首次矩阵复跑发现此处遗漏）；
  - 重试成功后的下一步骤停止时，报原故障码；
  - 真实预算不足仍报`budget_exceeded`；
  - 非服务方故障仍报`budget_exceeded`。
- **评测：** 服务方故障解析为`infra_error`，其他Provider失败仍为`system_error`；矩阵在新代码上复跑，五个场景断言全部通过。
