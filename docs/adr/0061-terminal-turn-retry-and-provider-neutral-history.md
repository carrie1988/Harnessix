---
doc_type: adr
status: current
version: 1
code_revision: edab546cc45ba6912a747494e7480a52118431cc
owners:
  - core
modules:
  - agent
  - session
  - context
  - models
related_adrs: []
related_tests:
  - tests/context
supersedes: []
---

# ADR 0061：终态Turn重试与Provider中立历史

- 状态：Accepted
- 日期：2026-09-08

## 背景

持久Coding Agent在Provider失败、宿主退出、用户取消或网络中断后需要继续未完成工作。原地重开终态Turn会破坏状态机、尝试账本和审计事实；直接重复原Prompt又可能诱导模型再次执行已经完成的写操作。会话在OpenAI-compatible与Anthropic之间切换时，若复用原生Tool Call ID、Thinking签名或流metadata，还会造成协议错配或把供应商授权性质的数据带入另一请求。

## 决策

1. `retry_turn`只允许重试Thread最新的`failed`、`cancelled`或`interrupted` Turn；每次重试创建新Turn，终态来源永不重开。
2. `TurnStarted`和`Turn`新增可选`retry_of_turn_id`，形成持久、可Replay的直接来源边；第一版不允许任意历史Turn或已完成Turn重试。
3. Retry写入固定续作User消息：`继续完成上一轮未完成的请求。不要重复已完成的副作用；先核对当前Workspace与持久效果事实。`，不复制原始Prompt。
4. 来源只要包含`outcome=unknown`的Tool Result，就以`retry_unsafe_effect`阻断自动重试。Retry不能代替效果对账。
5. 请求指纹在Retry时额外绑定`retry_of_turn_id`；普通Turn指纹算法保持逐字节兼容。幂等检查先于最新来源检查，提交成功后的同请求重试仍能返回原新Turn。
6. Reducer独立验证来源存在、是最新Turn、状态可重试且没有未知效果，防止伪造事件绕过Runtime。
7. Provider切换不新增供应商会话对象。`ModelAttempt`记录每次实际Provider/模型；模型历史只从规范Item构造，并为历史Tool Call生成稳定内部ID。
8. 历史`provider_call_id`、Thinking签名、流游标和请求metadata不跨Provider发送。双向真实Adapter MockTransport测试作为发布门禁。
9. Agent Event/Thread升级为v17，Session migration19只推进最低reader，不改写旧事实。

## 失败语义

| 场景 | 错误 | 状态变化 |
|---|---|---|
| 来源不存在 | `turn_not_found` | 无 |
| 来源不是最新Turn | `turn_retry_not_latest` | 无 |
| 来源为completed或非终态 | `turn_not_retryable` | 无 |
| 来源包含未知工具效果 | `retry_unsafe_effect` | 无 |
| Thread存在活跃Turn | `thread_busy` | 无 |
| Thread已归档 | `thread_archived` | 无 |
| request ID绑定不同Prompt、预算或来源 | `request_conflict` | 已有Turn保持不变 |
| 接受事务提交前退出 | 进程退出 | 完整回滚 |
| 接受事务提交后退出 | 进程退出 | 新Turn完整存在；启动恢复为interrupted |
| Provider切换历史无法映射 | `provider_protocol`或历史准备错误 | 发网前或模型步骤失败，不执行历史工具 |

## 后果

### 正向

- 终态单调、来源明确，Replay可以证明重试链；
- 不重复原Prompt，已完成工具结果继续作为事实供模型核对；
- 未知副作用不会因用户级Retry被自动越过；
- Provider切换无需供应商会话锁定，历史Tool配对保持稳定；
- 普通Turn的历史指纹和v1-v16事件导出保持兼容。

### 代价

- 自动Retry只能作用于最新Turn，历史分支探索需先Fork；
- 未知效果场景必须等待后续Reconcile能力或人工处理；
- 固定续作消息需要模型理解既有历史，不能用于缺失历史的外部导入；
- 供应商专有Thinking缓存不能跨Provider复用。

## 未采用方案

- **原地把终态Turn改回运行态**：破坏终态单调、账本索引和审计。
- **原样重复来源Prompt**：容易重复已经完成的副作用，也不能表达中断续作关系。
- **复制来源Turn并删除已完成工具**：改写事实且使Tool Call/Result历史失配。
- **允许未知效果继续**：把不可证明的外部状态交给模型猜测。
- **持久化供应商会话对象并跨Provider转换**：引入不稳定私有协议和签名泄漏。
