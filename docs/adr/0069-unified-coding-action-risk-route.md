# ADR 0069：统一 Coding Action 风险路由

- 状态：已接受
- 日期：2026-09-08
- 实施：0.7.5已完成（2026-09-09，[CI 34260423881](https://github.com/carrie1988/Harnessix/actions/runs/34260423881)）

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

## 实施结果

- `trusted_actions/contracts.py`实现宿主Binding、调用、规范资源、Route Plan、统一结果和审计事件合同；调用方没有effect/risk/policy/executor字段；
- `TrustedActionRouter`实现注册、strict JSON解析、资源解析、默认Policy、不可变Execution Plan、审批、执行前重开复核、恢复和reconcile；
- `SQLiteActionAuditStore`实现不可变Plan、当前投影和append-only哈希链；事件只保存结果/资源摘要，私有Plan payload为恢复保留规范化调用参数；
- `ExtensionActionPort`按source/source id限制能力，不暴露executor、Session、Secret或文件对象；
- `GitPushActionExecutor`与`ApprovedGitPushPolicy`证明外部写必须从统一Route进入，直接旧Action入口失败关闭；Push响应丢失后只读远端ref对账，不再次执行；
- 公共v1 Schema生成到`spec/`，实现与设计详见[0.7.5专项研究](../research/unified-action-plane-and-extension-boundaries.md)和[0.7详细设计](../m07-trusted-execution-and-delivery.md#14-075-action-plane与安全验收详细设计)。

## 后果与限制

- Execution Plan和Action Audit当前分属两个SQLite文件，跨库不做伪原子事务；前者先成功、后者失败只会留下不可达Plan，不能执行；
- 0.5历史Session事件和专用Patch/Process账本不改写，新Action Audit作为跨组件因果索引，不复制效果真相；
- 0.8以前不加载任意第三方进程内代码。扩展协议、生命周期和凭据产品化完成后，MCP/Skill/Hook adapter只能调用已冻结的`ExtensionActionPort`；
- 摘要和本地文件权限不抵抗同UID恶意宿主进程，远端认证链路也不在0.7本地bare remote验收范围内。
