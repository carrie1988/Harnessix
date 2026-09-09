# 0.7.5 统一 Action Plane 与扩展边界源码研究

- 状态：已冻结
- 冻结日期：2026-09-09
- 适用范围：Harnessix Code 0.7.5
- 研究方法：锁定本地源码提交，只提取机制、不变量和失败语义，不复制参考实现

## 1. 研究问题

本轮只回答四个与0.7.5实现直接相关的问题：

1. 内置工具、自定义工具、MCP、Skill和Hook如何进入同一工具路由；
2. Schema、工具身份、权限、Sandbox和执行器应由谁持有；
3. 扩展“允许”能否覆盖宿主deny，以及扩展能获得哪些能力对象；
4. 外部非幂等副作用在返回丢失或宿主崩溃后如何避免重复执行。

证据等级沿用0.7研究基线：锁定源码可直接复查的内容记为“事实”；跨入口归纳记为“推断”；Harnessix自己的取舍记为“决策”。

## 2. 冻结基线

| 项目 | 锁定提交 | 本轮主要证据 |
|---|---|---|
| Codex | `d6489472f3c15e87d2d7763a5fde033545c530f8` | `codex-rs/core/src/tools/router.rs`、`registry.rs`、`orchestrator.rs` |
| OpenCode | `d6855b6b47a8433462ac6aeeba882ccf734cb7f1` | `packages/opencode/src/tool/tool.ts`、`registry.ts`、`shell.ts`、`external-directory.ts` |
| Claude Code本地非官方重建源码 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | `src/Tool.ts`、`src/services/tools/toolExecution.ts`、`toolHooks.ts` |

Claude Code材料不是Anthropic官方开源仓库，只作为交叉行为线索，不能单独支撑协议兼容或安全结论。

## 3. Codex求证

### 3.1 Tool Router和Registry是宿主控制面

**事实**

- `router.rs`的`ToolRouter`同时持有向模型广告的Tool Spec和实际Runtime Registry，并把Provider返回的function/custom/local-shell调用先规范化为`ToolCall`，再按名称查找执行器；未知工具不会获得执行能力。
- `registry.rs`以`CoreToolRuntime`统一不同工具运行时，注册时区分trusted与external，但二者仍进入同一个Registry处理链；工具执行前后hook、telemetry和结果转换集中发生，不由模型调用参数选择。
- MCP工具也通过Registry/Core Runtime进入相同路由，不直接从Provider响应调用远端工具。

**推断**

Tool描述和执行器必须成对由宿主注册。只校验模型返回的工具名称或Schema不足以证明“广告的工具”和“执行的代码”是同一能力。

### 3.2 Approval、Sandbox与网络所有权集中编排

**事实**

- `orchestrator.rs`明确把执行顺序组织为approval、Sandbox选择、首次尝试、必要时按策略升级；工具Runtime不能自行跳过该顺序。
- 文件系统Permission先物化为Sandbox策略，再计算是否需要审批；网络批准与文件系统批准是独立事实。
- attachment/owner持有的网络策略不能被Sandbox escalation绕过；即使某次工具请求希望扩大权限，宿主所有者策略仍然优先。

**Harnessix决策**

`TrustedToolBinding`、规范资源、`ExecutionPlanV2`、Approval Checkpoint和executor identity必须由宿主冻结。扩展只能提交调用意图，不能提交effect class、risk level、Sandbox、Secret或Policy结果。

## 4. OpenCode求证

### 4.1 一个Registry聚合多来源工具

**事实**

- `tool/registry.ts`把builtin、custom和plugin工具组装为同一集合，并在广告前执行Schema转换；`tool/tool.ts`统一参数Schema、调用Context、结果和截断处理。
- Shell工具在spawn前解析命令涉及的路径和外部目录，再通过`ctx.ask`提交permission pattern；其他工具也复用同一ask协议。
- `external-directory.ts`把Workspace外路径作为独立权限对象，而不是把一次Workspace授权扩展为任意绝对路径。

### 4.2 可借鉴机制与不能照搬的边界

**事实**

- Plugin Tool执行时会得到包含session、message、agent、abort和permission ask等信息的`PluginToolContext`，扩展能力较宽。
- Permission等待主要是运行期对象；这不等同于副作用账本或崩溃可恢复Approval Checkpoint。

**Harnessix决策**

- 借鉴“多来源同一Registry”和“资源先规范化再询问”；
- 不把Session Store、Host Executor、Secret Provider、文件系统对象或通用spawn交给扩展；
- 不以运行期Promise/Map作为审批真相，审批继续绑定不可变Plan fingerprint并持久化。

## 5. Claude Code本地重建源码交叉求证

### 5.1 工具合同与固定执行序列

**事实线索**

- `src/Tool.ts`的工具合同把`validateInput`、`checkPermissions`、只读/破坏性/open-world判断及并发能力分开表达。
- `services/tools/toolExecution.ts`的主链依次包含Schema解析、`validateInput`、PreToolUse Hook、permission解析、`tool.call`和PostToolUse Hook/telemetry。
- MCP工具默认也进入permission passthrough，而不是因来源是MCP自动获得宿主权限。

### 5.2 Hook不能提升宿主deny

**事实线索**

- `toolHooks.ts`明确保持不变量：PreToolUse Hook的allow不能覆盖settings中的deny/ask；宿主规则仍需重新检查。

**Harnessix决策**

Hook只可缩小、拒绝或提供建议，不可把deny改为allow，不可替换executor identity。Hook和Skill未来作为`source=hook/skill`调用时仍只能获得`ExtensionActionPort`。

## 6. 三者共同机制与剩余缺口

共同机制是：Tool由宿主注册、调用先解码、权限先于执行、多来源尽量复用路由、Hook不能天然成为更高权限主体。参考实现重点解决交互式编码体验，但不能直接给出Harnessix需要的全部生产语义：

- Tool风险标签可能仍来自工具实现自身，缺少独立的host binding digest；
- 审批与完整Workspace/环境/Secret/Sandbox快照未必形成一个不可变跨进程合同；
- 本地或外部非幂等效果通常没有统一`UNKNOWN → reconcile`账本；
- 扩展Context可能包含过宽宿主对象；
- Tool输出审计与正文脱敏/Artifact边界未必是同一强制入口。

## 7. Harnessix 0.7.5独立设计结论

### 7.1 唯一可信路由

调用链固定为：

```text
CodingActionInvocation
  → 宿主TrustedToolBinding精确匹配
  → strict JSON Schema解析
  → ResourceResolver生成规范资源
  → DefaultCodingRiskPolicy
  → Workspace Snapshot + ExecutionPlanV2
  → Approval Checkpoint（如需要）
  → 执行前重开并逐项复核
  → executor
  → 摘要化append-only Audit Event
  → 终态或UNKNOWN/reconcile
```

`CodingActionInvocation`不包含effect、risk、policy、Sandbox、Secret值或executor。相同工具名但来源、来源实例、版本、Schema摘要、Tool fingerprint或Binding digest不同，均是不同能力。

### 7.2 规范资源与策略

资源由宿主resolver产生，调用方只能提供业务参数：

- `workspace/read|write|execute`；
- `process/execute`；
- `network/connect`；
- `secret/use`；
- `git_ref/read|update`；
- `external/read|write`。

标识和属性只以SHA-256进入公开审计。只读低风险且无进程、网络、Secret或写访问可自动允许；写入、medium/high、网络、Secret和外部reconcile必须审批；critical/destructive默认拒绝。资源与effect、网络Sandbox或Secret bindings不一致时失败关闭。

### 7.3 持久化与崩溃语义

Execution Plan Store保存不可变Plan和一次性Approval；Action Audit Store保存不可变Route Plan、当前投影和哈希链事件。执行前重开两者并核对完全相等，同时重新验证Workspace Snapshot和当前注册Binding。

宿主在`running/reconciling`退出时，重开后只能进入`unknown`，不得直接重放。只读错误可以失败；写入或外部调用出现未分类异常时保守为unknown。只有绑定了`durable_ledger`或`external_reconcile`的工具可对账，无对账能力转人工处置。

### 7.4 Extension最小能力

`ExtensionActionPort`只暴露本来源的Binding列表、plan、execute、reconcile和status；来源和source id在端口创建时固定。它不暴露Router内部Registry、executor、Session、Secret、Workspace对象或文件句柄。MCP、Skill、Hook和custom不能读取或执行其他来源的Plan。

### 7.5 Git Push证明切片

Git Push选作第一个真实外部非幂等写：

- Intent绑定repository digest、规范remote URL摘要、单个本地/远端branch ref、local OID、expected remote OID、force mode和幂等键；
- Commit不会隐式产生Push权限；Push必须形成新的高风险Plan和独立批准；
- 固定argv只更新一个ref，关闭Hook和prompt，限制Git协议，并始终使用exact `--force-with-lease`；
- Push调用开始后的异常一律按不确定效果处理；reconcile只读取远端ref，目标OID已存在则成功，仍为旧OID则失败，第三方OID则人工处置；
- 当前受控真实验收使用本地bare remote证明CAS、旁路拒绝和零重复副作用。HTTPS/SSH非交互凭据装配属于0.9.5独立Git凭据与Dogfooding切片，不能复用0.8.6模型Provider Secret；0.7不继承用户完整环境或把Token写入argv。

## 8. 验证映射

| 风险 | 确定性证据 |
|---|---|
| 多来源策略漂移 | builtin/MCP/Skill/Hook/custom参数化同策略测试 |
| 伪造effect、版本或Schema | extra字段、未注册工具、版本/Schema替换失败关闭 |
| 扩展越权 | source/source id隔离及无privileged object测试 |
| 审批后漂移 | 参数指纹、Workspace Snapshot、Git remote配置变化测试 |
| 宿主硬退出 | 子进程在`running`执行效果后`os._exit`，重开只reconcile且效果计数为1 |
| 外部返回丢失 | 真实bare remote已更新后注入响应丢失，状态为unknown，对账成功且不二次Push |
| 直接绕过Action Router | 直接调用旧ActionService被`ApprovedGitPushPolicy`拒绝且remote无ref |
| Git命令参数与输出歧义 | remote/ref在子进程前准入；非法选项、branch ref、URL主体、控制字符和多行响应失败关闭，LF/CRLF均有确定解析 |
| 审计篡改 | SQLite冗余列、payload、自摘要和事件链任一不一致失败关闭 |

## 9. 不采用的方案

- 不把模型返回的effect/risk作为Policy事实；
- 不让每个工具自行决定是否审批；
- 不给扩展通用Python对象、任意文件句柄或spawn能力；
- 不把Hook allow解释为覆盖宿主deny；
- 不按异常自动重试外部写；
- 不把Git Push并入Commit或根据upstream隐式授权；
- 不宣称摘要是签名、SQLite能抵抗同UID恶意进程，或本地bare remote等价于远端认证链路。
