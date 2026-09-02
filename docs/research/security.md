# Permission、Approval 与 Sandbox 研究

## 1. 研究基线

见[源码研究基线](baselines.md)。本主题关注“谁有权让 Agent 做什么”，不把模型提示词中的安全要求当成执行边界。

## 2. 参考实现事实

### 2.1 Codex

**事实**

- Exec Policy 综合规则匹配、危险命令判断、审批策略和 Sandbox 能力；
- 复合命令按片段解析，只有所有片段都明确允许时才可绕过 Sandbox；
- 未匹配命令、危险命令和缺少真实 Sandbox 的情况不会静默视为安全；
- 安全但未明确允许的命令可以依赖受限 Sandbox 运行；
- Exec Request 显式携带网络、Sandbox 和执行环境信息；
- 命令与网络规则可以形成策略修订。

**推断**

Codex 将“是否允许”和“在哪里执行”分开：Approval 是授权，Sandbox 是能力限制，两者不能互相替代。

### 2.2 OpenCode

**事实**

- Permission Rule 由 action、resource 和 allow/deny/ask effect 构成；
- 匹配采用有序规则，后匹配的 wildcard 规则生效；未匹配默认 ask；
- Ask 请求可以 once、always 或 reject；
- always 会保存项目范围规则，并解决被新规则覆盖的等待请求；
- External Directory 使用 canonical path 检测；
- 当前 pending approvals 保存在进程内 Map，因此本提交本身不提供审批等待的崩溃持久性；
- Bash 对命令参数路径的识别存在启发式 TODO，且 Host Shell 明确拥有当前用户权限。

**推断**

规则系统易理解，但 Host 模式路径分析无法提供强隔离。Harnessix 应借鉴交互语义，不复用其进程内审批状态。

### 2.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

- Tool 元数据可表达只读、破坏性、并发安全、可中断和开放世界；
- 未声明工具不会自动获得“只读/安全并发”属性；
- Permission Check 位于 Tool 执行契约中。

非官方仓库不足以证明实际 OS Sandbox 或完整权限实现。

## 3. Harnessix 安全模型

### 3.1 信任层级

由高到低：

~~~text
Runtime hard rules
  > 用户显式会话策略
  > 已信任的用户配置
  > 项目策略
  > 项目指令 / Skills / Hooks
  > 模型输出
  > 仓库内容、Tool 输出、网络和 MCP 数据
~~~

低层输入不能修改高层安全边界。项目中的 AGENTS.md、README、测试夹具和代码注释都可能包含 Prompt Injection。

### 3.2 Permission 与 Approval

- **Permission**：策略引擎对规范化效果的决定；
- **Approval**：用户对一次不可变效果指纹的授权；
- **Sandbox**：即使获批也不能突破的 OS 能力边界；
- **Action Plane**：高风险外部效果的持久执行与对账。

审批指纹至少绑定：

~~~text
tool name/version/source
normalized arguments
cwd + workspace identity/revision
environment digest（不含 Secret 明文）
effect/risk
sandbox/network profile
policy version
idempotency key
~~~

任一字段变化都必须重新判定；UI 展示内容与实际执行指纹来自同一规范化对象，防止 bait-and-switch。

### 3.3 执行后端

| 后端 | 安全声明 | 用途 |
|---|---|---|
| host | 当前用户权限下的受限执行，不宣称强隔离 | 低风险、本地开发、用户明确批准 |
| container | 文件系统、进程、资源和网络受控 | 不可信命令和更强隔离需求 |
| durable action worker | 策略、租约、幂等、UNKNOWN/Reconcile | 外部系统写操作 |

隔离后端不可用时默认 fail closed；只有用户策略显式允许，才能降级到标记清楚的 host 模式。

## 4. 文件系统边界

1. 请求路径先做词法规范化，再解析真实路径；
2. 检查 Workspace Root 和允许的 External Root；
3. 对写操作记录父目录与目标文件身份；
4. 打开与替换阶段防御 symlink/rename TOCTOU；
5. 原子写入使用同目录临时文件、fsync 和 replace；
6. 不跟随未知符号链接写出 Workspace；
7. 审批后 Workspace Revision 变化时重新校验；
8. Git 脏文件默认不覆盖，Patch 必须报告冲突。

仅使用 `Path.resolve() + startswith` 不足以消除执行时竞态。

## 5. Process 与网络边界

- Shell 不通过字符串拼接注入参数；
- 启动独立进程组，超时/取消时终止完整进程树；
- stdin 默认关闭，PTY 是显式能力；
- stdout/stderr 有内存上限，完整输出转 Artifact；
- 环境变量采用 allowlist，Secret 按 Tool 最小化注入；
- 网络默认按 Sandbox Profile 控制，不依赖命令文本识别；
- 域名策略同时处理 DNS、IPv4/IPv6、代理和重定向；
- 后台进程不得在 Turn 终态后继续遗留。

## 6. Extension 边界

- MCP、Skills、Hooks 和自定义 Tool 都有来源、版本和信任状态；
- Tool Schema 变化使旧 Approval 失效；
- Extension 不能直接获得 Session Store、Secret Store 或 Host Executor；
- 所有 Tool 最终经过同一 Registry、Permission、Sandbox 和审计；
- Hook 的 post 阶段不能宣称撤销已发生效果；
- 未信任项目 Hook 默认不加载。

## 7. 已识别失败模式

- Prompt Injection 诱导读取 Secret 或修改策略；
- 路径穿越、符号链接和 TOCTOU 逃逸；
- Shell 拼接、命令替换和后台进程逃逸；
- 禁网策略被 DNS、IPv6 或代理绕过；
- 审批 UI 与执行参数不一致；
- Tool/MCP 更新后复用旧授权；
- Provider 错误、日志、Trace 或 Artifact 泄漏凭据；
- 进程崩溃后重复外部写；
- 客户端伪造 Tool Result 或 Approval；
- Sandbox 不可用时静默降级。

完整资产、攻击者、控制和剩余风险见[威胁模型 v1](../threat-model.md)。

## 8. 对应测试

- Prompt Injection 红队语料；
- `..`、绝对路径、symlink 链和 rename race；
- Approval 指纹字段变更属性测试；
- Shell 超时、子进程/孙进程和后台任务清理；
- 网络禁用下 DNS、IPv4/IPv6、代理和 redirect；
- Secret canary 全链路扫描；
- 恶意 MCP Schema、Skill 与 Hook；
- Sandbox 不可用和权限规则冲突；
- 外部写在请求前、提交后、结果前逐点崩溃。

## 9. 源码索引

- Codex：[exec_policy.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/exec_policy.rs)、[sandboxing/mod.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/sandboxing/mod.rs)
- OpenCode：[permission schema](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/schema/src/permission.ts)、[permission runtime](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/permission.ts)、[location-mutation.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/location-mutation.ts)
- Claude 逆向仓库：[Tool.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/Tool.ts)
