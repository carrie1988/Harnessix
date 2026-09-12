---
doc_type: change-design
status: historical
version: 1
code_revision: 1cb15efdd154f16e0f894e70998d26670ca60d04
owners:
  - core
modules:
  - agent
  - session
  - context
  - models
related_adrs:
  - docs/adr/0061-terminal-turn-retry-and-provider-neutral-history.md
related_tests:
  - tests/context/test_turn_retry_provider_switch.py
  - tests/context/test_turn_retry_recovery.py
supersedes: []
---

> **历史专题设计**：本文保留0.6.5终态Turn Retry、Interrupted Recovery与Provider切换的增量设计，不再作为当前实现的唯一事实源。当前Retry入口、状态机和恢复语义见[Agent Runtime模块](modules/agent.md)，事务与迁移见[Session模块](modules/session.md)，Provider中立历史分别见[Context模块](modules/context.md)与[Model Runtime模块](modules/models.md)。

# Turn Retry、Interrupted Recovery 与 Provider 切换详细设计

- 版本：0.6.5
- 更新日期：2026-09-08
- 状态：实现、本地完整验收及远端CI全部通过

## 1. 目标与边界

本设计为持久Thread提供终态Turn续作和Provider切换，保持终态单调、工具效果安全、请求幂等、历史协议正确及完整审计。实现不包含未知效果自动Reconcile、任意历史Turn重放、自动模型路由、跨节点任务接管或供应商Thinking缓存迁移。

## 2. 模块

| 模块 | 职责 |
|---|---|
| `agent/models.py` | `retry_of_turn_id`领域字段、Event v17兼容边界 |
| `agent/reducer.py` | Retry来源、终态、最新Turn和未知效果Replay校验 |
| `agent/runtime.py` | `retry_turn`公开入口、幂等接受、固定续作输入和执行 |
| `agent/telemetry.py` | 低基数`retry`操作Span、Counter和结果 |
| `models/_history.py` | 规范Item到稳定内部Tool Call ID的Provider中立历史 |
| OpenAI/Anthropic Adapter | 把同一规范历史分别映射成目标供应商协议 |
| `session/sqlite.py` | Event/Projection v17持久化、恢复和migration19门禁 |

## 3. 领域契约

### 3.1 Retry来源

`retry_turn(thread_id, source_turn_id, request_id, budget=None, trace_context=None)`要求：

- Thread未归档且没有活跃Turn；
- 来源是`thread.turns[-1]`；
- 来源状态为`failed`、`cancelled`或`interrupted`；
- 来源全部完成Tool Result中不存在`outcome=unknown`；
- `request_id`、Budget和来源共同形成幂等输入。

来源Turn保持原终态、错误、Item、模型尝试、Usage和完成时间。新Turn使用新UUID并在`retry_of_turn_id`记录直接来源；连续Retry形成可审计链，不将整条链复制到单字段。

### 3.2 续作输入

新Turn固定写入一份完成User Item：

```text
继续完成上一轮未完成的请求。不要重复已完成的副作用；先核对当前Workspace与持久效果事实。
```

原始请求已存在于规范模型历史。固定续作消息表达恢复意图但不声称工具状态，模型必须依据持久Tool Result和当前Workspace工具重新核对。Runtime不会重新投递来源Tool Call，也不会把来源调用加入新Turn的`pending_calls`。

### 3.3 幂等与冲突

普通Turn维持既有`SHA256({prompt,budget})`算法。Retry使用`SHA256({prompt,budget,retry_of_turn_id})`。接受事务在验证“来源仍是最新”之前查询同`request_id`：

- 指纹与`retry_of_turn_id`均一致：返回已存在Turn，不新增事件；
- 指纹或来源不同：`request_conflict`；
- 尚不存在：再验证当前来源并原子提交TurnStarted及User Item。

该顺序覆盖提交后响应丢失：新Turn已经成为最新Turn时，相同Retry请求仍可找到原结果。

## 4. 状态与恢复

```text
failed/cancelled/interrupted source
  └─ retry_turn ─→ new accepted Turn ─→ normal Agent Loop

waiting_approval/waiting_action
  └─ resume_turn ─→ same Turn

accepted/preparing/calling/executing/finalizing/cancelling after restart
  └─ startup recovery ─→ same Turn interrupted
       └─ retry_turn ─→ new Turn
```

Retry不恢复旧Provider流、异步Task或内存CancelToken。启动恢复仍以现有Session恢复事务写入`interrupted`；再次打开Runtime不自动调用Provider。

## 5. Provider中立历史

Session中的User、Assistant、Tool Call和Tool Result是规范事实。每次模型请求按目标Adapter重新映射：

- 历史Tool Call ID使用`call_<UUID hex>`稳定内部表示；
- Tool Result引用同一内部ID；
- OpenAI-compatible输出`assistant.tool_calls`和`role=tool`；
- Anthropic输出`tool_use`和`tool_result` block；
- 历史响应中的原生`provider_call_id`只用于原步骤观测，不进入后续请求；
- Thinking签名、流事件ID、请求metadata和SDK对象不持久化为跨Provider历史。

Provider切换只需用同一SessionStore和新Provider打开Runtime。历史工具结果可见，但来源Turn已终结且不属于新Turn的待执行集合，因此不会重执行。

## 6. 事务、取消与故障恢复

| 故障窗口 | 预期事实 |
|---|---|
| `session.after_events`前/处退出 | 接受事务回滚，不存在Retry Turn |
| `session.after_projection`退出 | Event与Projection一并回滚 |
| `session.after_commit`退出 | Retry Turn及User Item完整存在 |
| 提交后、Provider请求前退出 | 启动恢复将Retry Turn收敛为interrupted；零Provider请求 |
| Provider流中断 | 当前Retry Turn按既有账本失败或中断；来源不变 |
| 用户取消 | 当前Retry Turn按既有取消协议终结；已提交事实不删除 |

所有恢复均通过事件Replay和Projection重建验证，不根据进程退出位置猜测是否提交。

## 7. 安全边界

- `retry_of_turn_id`不是执行授权，只是来源关系；
- 未知效果在接受前失败关闭；
- 已知成功效果保留为历史事实，后续写操作仍经过正常revision、Policy、Approval和Action边界；
- Provider切换不携带旧供应商签名、认证、原生调用ID或流状态；
- Metric不包含Prompt、路径、请求ID、模型响应、Tool参数或供应商原生ID。

## 8. 持久化与兼容

- Agent Event/Thread：v17；
- Session migration：19；
- v1-v16 Event Schema保持冻结；旧版本序列化`TurnStarted`时移除新增空字段；
- migration19不改写旧Event、Projection、Artifact、Compaction窗口或模型尝试；
- v16→v17独立wheel验证旧字节不变、新Retry可Replay/Rebuild、v16 reader失败关闭。

migration19 SHA256为`926e3bbb1ee98971815166b9737032b8bc63ace9d6fb84bc887380606d654c7a`。独立升级使用提交`24e0899`构建的v16 wheel和当前v17 wheel，SHA256分别为`e43338aa23c0da5d03a7fcfef1cc32c6fcff0e7c2da18f713cdc8f9ddba4fd2c`、`40c7b59fed4c81746b643a2639aa292a1039c28f4eb1fc3a5a6fbfa928ea7338`；`scripts/turn_retry_upgrade_probe.py`的`create → upgrade → old-reader`三阶段全部通过。

## 9. 可观测性

Retry复用现有Operation协议，新增`operation=retry`：

- Span：`harnessix.agent.retry`；
- Counter：`harnessix.agent.operation{operation=retry,outcome,category?}`；
- Histogram：既有operation duration；
- outcome使用有限集合`completed/failed/cancelled/interrupted/...`；
- Turn来源关系从持久投影查询，不进入Metric标签。

每个模型尝试继续记录实际Provider、requested model、response model、Usage与Billing，支持切换前后Cost聚合。

## 10. 验收矩阵

1. failed/cancelled/interrupted最新Turn创建新Turn，来源终态不变；
2. completed、非最新、活跃、归档和未知效果来源失败关闭；
3. 相同请求幂等，不同普通/Retry输入冲突；
4. Replay与Rebuild拒绝伪造Retry事件；
5. 接受事务三个SQLite真实进程退出切点；
6. OpenAI-compatible→Anthropic及Anthropic→OpenAI真实Adapter MockTransport双向切换；
7. 历史Tool Call/Result配对，旧供应商调用ID不出现在新请求，工具只执行一次；
8. 长会话串联Compaction、Restart、Retry、Provider切换、Fork与Archive；
9. v16→v17独立wheel升级、Schema冻结、旧reader拒绝；
10. Ruff、Mypy、严格全量测试、默认CI四矩阵和文档同步。

本地严格全量结果为2924 passed、2 skipped，270.46秒；两个skip仅因未配置`HARNESSIX_TEST_POSTGRES_URL`。Ruff格式与规则、Mypy严格检查164个源文件均通过。Schema连续生成两次聚合SHA256均为`68f1eed44d4e8dee742db5adfd01f85f4f6844d2509b18c6ccce6b4744151f0c`。实现提交的[CI 34192389373](https://github.com/carrie1988/Harnessix/actions/runs/34192389373)已通过Python 3.12、Python 3.13、macOS Coding Tools和PostgreSQL四项任务，0.6.5正式关闭。
