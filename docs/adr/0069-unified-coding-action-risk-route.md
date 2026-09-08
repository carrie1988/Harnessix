# ADR 0069：统一 Coding Action 风险路由

- 状态：已接受
- 日期：2026-09-08

## 背景

当前只读工具、Patch、Patch Batch 和 Process 使用各自桥接。继续为 Sandbox、Git、MCP 和 Hook 增加旁路会形成多套授权和恢复语义。

## 决策

1. 所有具有文件、进程、网络、Secret 或外部副作用的 Tool 先规范化为 `ExecutionIntent`，再由唯一 Planner 生成 `ExecutionPlan`。
2. Policy 输入使用规范资源和效果，不信任 Tool 自报风险；结果固定为 deny、allow、require approval，并记录规则、策略版本和理由代码。
3. Approval 只授权一个 plan fingerprint；执行器必须声明可落实的 Sandbox/Network/Secret 能力并再次匹配计划。
4. Audit 记录 plan id/fingerprint、Tool 来源、资源摘要、策略、批准、executor、阶段、输出/Artifact 摘要和 reconcile 结论，不记录 Secret 明文。
5. 本地文件/进程效果使用专用 durable ledger；外部非幂等写进入既有 Action Plane，并保持 `UNKNOWN → reconcile`，禁止按异常自动重放。
6. 内置 Tool、MCP、Skill、Hook 和未来扩展必须经同一入口；Extension 只能获得受限端口，不能直接持有 Host Executor、Session DB 或 Secret Store。

## 兼容策略

- 0.5 的 Patch/Process 桥接作为 legacy adapter 接入新 Planner，不重写历史事件；
- 新公共契约独立版本化；旧 Session reader 遇到新执行事件必须拒绝接管，而非丢字段继续；
- 0.8 开放协议和扩展前，统一入口必须已由 0.7.5 攻击测试证明不可旁路。

## 验证

- 每种 Tool 来源执行等价风险路由；
- 伪造低风险、自改 Schema/版本、直接调用 executor 均失败；
- 外部写在提交后丢响应时只进入 reconcile，不重复执行；
- 审计与最终文件、进程、Git 和远端事实一致。

