# Turn Retry、Interrupted Recovery 与 Provider 切换源码研究

- 更新日期：2026-09-08
- 研究基线：Codex `a0dcfe2`、OpenCode `69c172e`、Claude Code Source `2ca5dda`
- 研究范围：终态Turn重试、中断续作、模型切换、历史归一化与副作用安全

## 1. 研究问题

0.6.5需要回答以下问题：

1. 失败、取消或中断的Turn应原地重开，还是创建具有来源关系的新Turn；
2. Runtime进程退出后，哪些状态允许同Turn继续，哪些状态只能终结后重试；
3. 重试是否重新发送原始用户输入，如何防止已完成副作用被再次执行；
4. OpenAI-compatible与Anthropic之间切换时，历史Tool Call/Result如何保持语义配对；
5. 供应商签名、调用ID和流状态能否跨模型边界复用；
6. 长会话经历压缩、恢复、重试、模型切换、Fork与Archive后，事实、费用和审计是否仍一致。

## 2. Codex证据

### 2.1 终态工作不原地重开

`codex-rs/tui/src/app/safety_buffering.rs`的快速模型重试先确认被中断Turn是最新Turn且已经停止，再读取有界完整输入，在失败Turn之前Fork历史，最后把原输入作为子Thread中的新Turn提交。`codex-rs/app-server-protocol/src/protocol/v2/turn.rs`把`completed`、`interrupted`、`failed`和`inProgress`作为互斥Turn状态。

可复用结论：终态Turn不可重新变成运行态；重试必须有新的Turn身份，并显式绑定稳定来源。

### 2.2 模型切换是新请求事实

Codex允许在Turn启动参数上覆盖模型和推理强度；`codex-rs/thread-store/src/local/rollout_migration/rollback.rs`识别持久化的`<model_switch>`上下文消息。模型切换不会恢复旧HTTP流或旧Turn执行器。

可复用结论：模型身份属于每次模型尝试；历史语义可以延续，但供应商请求状态不能延续。

## 3. OpenCode证据

### 3.1 Provider尝试重试与用户级Turn重试分层

`packages/opencode/src/session/retry.ts`只处理限流、服务端错误和连接类暂态故障，显式排除Context Overflow；`session/processor.ts`在中断时把活动工具调用标记为错误并终结Assistant消息。`session/prompt.ts`的每个步骤从最新User消息选择模型。

可复用结论：Adapter内部的无副作用HTTP尝试重试不能替代持久Turn Retry。已经进入工具或持久语义阶段的工作必须通过新Turn恢复。

### 3.2 历史去供应商化

`packages/opencode/src/session/message-v2.ts`在当前模型与历史Assistant模型不同时，不回放原供应商的metadata。模型签名和请求专用字段不被当作跨模型语义事实。

可复用结论：跨Provider历史只保留User、Assistant、Tool Call和Tool Result的供应商中立语义；原生调用ID、签名和流metadata不得跨边界发送。

## 4. Claude Code Source证据

### 4.1 中断恢复使用显式续作输入

`src/utils/conversationRecovery.ts`检测中断Prompt、未配对Tool Use和孤立Thinking，并把可续作中断转换为新的`Continue from where you left off.`用户输入。`src/cli/print.ts`在启用恢复时移除中断标记并只重新入队一次。

可复用结论：续作应作为新的、可审计的用户级语义输入，不重复伪造原Turn，也不能保留未配对工具历史。

### 4.2 模型Fallback清理模型绑定状态

`src/query.ts`在模型Fallback时重置Assistant/Tool临时数组、丢弃旧流执行器并移除与模型绑定的Thinking签名；`src/utils/messages.ts`另行保存模型切换痕迹。

可复用结论：切换Provider前必须丢弃请求级临时状态。持久账本记录实际Provider/模型，模型历史使用稳定内部身份重新映射。

## 5. Harnessix决策

1. `resume_turn`继续只处理`waiting_approval`和`waiting_action`持久边界。其他进程内活动状态在启动恢复时收敛为`interrupted`，不恢复旧Provider流。
2. `retry_turn`仅接受当前Thread最新的`failed`、`cancelled`或`interrupted` Turn，并创建新Turn；`completed`、非最新来源、活跃Turn和归档Thread均拒绝。
3. 新Turn以`retry_of_turn_id`持久绑定来源，使用固定续作输入，不复制原始Prompt。原Prompt、已完成工具结果和错误仍位于规范历史中。
4. 来源包含`ToolResult.outcome=unknown`时拒绝自动重试。调用方必须先通过后续Reconcile能力确认效果，不能以新Turn绕过不确定副作用边界。
5. 相同`request_id`和相同重试输入幂等返回同一新Turn；同一`request_id`被普通Turn或另一来源占用时明确冲突。
6. Provider可在Runtime重新打开时替换。每个`ModelAttempt`继续记录实际Provider与模型；历史仅从规范Item构造，Tool Call使用稳定内部ID重新映射，不发送历史供应商调用ID。
7. 真实双Adapter测试必须双向验证OpenAI-compatible与Anthropic切换、Tool Call/Result配对、历史供应商ID不泄漏和工具不重执行。
8. 长会话综合验收串联Compaction、进程恢复、Retry、Provider切换、Fork与Archive，并核对Replay/Rebuild、原始事件、活动窗口、Usage和Cost。

## 6. 差异化结论

Harnessix把三种容易混淆的机制分离：Provider内部暂态尝试重试、持久等待边界上的同Turn Resume，以及终态来源上的新Turn Retry。跨Provider历史由规范Item重新映射，供应商专有身份不进入下一Provider；不确定外部效果则阻断自动续作。该设计优先保证可恢复、可审计和副作用安全，而不是以“继续生成”掩盖状态与效果不确定性。
