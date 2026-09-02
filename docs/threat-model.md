# Harnessix Code 威胁模型 v1

- 状态：0.2 架构基线
- 日期：2026-09-02
- 适用范围：本地优先 CLI、Headless App Server、Agent Runtime、Coding Tools、Session Store、Action Plane

## 1. 安全目标

Harnessix Code 必须保证：

1. 未授权的模型、项目内容和扩展不能直接获得文件、进程、网络或 Secret 能力；
2. 用户批准的内容与实际执行效果一致；
3. Workspace 外读写有明确策略；
4. 取消、超时和崩溃不留下不可解释的进程或副作用；
5. 未知外部写不会被自动重复；
6. Prompt、日志、Trace、Session 和 Artifact 不泄漏凭据；
7. 客户端不能伪造 Runtime 事实；
8. Sandbox 不可用时不静默降级。

安全边界研究见[Permission、Approval 与 Sandbox](research/security.md)。

## 2. 范围与假设

### 范围内

- 本地用户启动的 Harnessix 进程；
- stdio JSON-RPC Client；
- Model Provider 网络连接；
- Workspace 文件、Git 和 Process Runtime；
- 内置 Tool、MCP、Skills 和 Hooks；
- SQLite Session Store；
- Harnessix Action Plane 与外部系统；
- 日志、Trace 和 Artifact。

### 假设

- 操作系统和当前用户账号未被预先完全攻陷；
- 用户可能打开恶意或被污染的代码仓库；
- 模型输出和所有外部内容都不可信；
- Host Executor 与当前用户共享权限，不宣称强隔离；
- Container/Sandbox 只能提供其后端真实支持的能力；
- Model Provider、MCP Server 和依赖可能发生供应链或服务端失陷。

### 非目标

- 防御拥有 root/内核权限的本地攻击者；
- 证明第三方模型不保留发送给它的数据；
- 在 0.2 实现完整 DLP、远端多租户和企业 KMS；
- 通过命令文本静态分析证明任意 Shell 安全。

## 3. 资产

| 资产 | 影响 |
|---|---|
| Workspace 源码和未提交修改 | 机密性、完整性、可恢复性 |
| Git 凭据、SSH Key、云凭据、API Key | 高机密性和外部系统权限 |
| 本机文件、进程和网络 | Workspace 外横向影响 |
| Session/Event/Approval 数据 | 审计、隐私和恢复正确性 |
| Tool/Policy/Sandbox 配置 | 执行边界完整性 |
| Artifact、日志和 Trace | 可能包含源码、输出和 Secret |
| 外部 SaaS/DB 资源 | 不可逆或计费副作用 |
| 发布包、插件和依赖 | 供应链完整性 |

## 4. 信任边界

~~~text
用户 / CLI
   │  标准 JSON-RPC，输入校验
   ▼
App Server ───────────── Session Store / Artifact
   │                         │
   ▼                         │
Agent Runtime                │
   ├── Context Engine ◄──── 项目指令、仓库内容（不可信）
   ├── Model Runtime  ◄──── Provider（不可信响应）
   └── Tool Runtime
         ├── Permission / Approval
         ├── Host or Container Sandbox
         ├── Local OS / Workspace
         ├── MCP / Hook / Skill（扩展边界）
         └── Action Plane ── 外部 SaaS / DB
~~~

每次跨边界都必须有 Schema、身份、大小、权限和审计控制，不能因数据来自“Agent 自己”而省略校验。

## 5. 攻击者与入口

### 攻击者

- 恶意仓库作者；
- 被 Prompt Injection 污染的网页、Issue、Tool 输出；
- 恶意或失陷的 MCP/Plugin/Hook；
- 失陷 Provider；
- 同一用户权限下的恶意本地进程；
- 构造恶意协议消息的 Client；
- 利用重复请求和崩溃窗口的外部调用方。

### 主要入口

- User Message 与项目指令；
- 文件内容、Git Diff、测试输出；
- Provider Stream；
- Tool 参数和 Shell 命令；
- MCP Schema/Result；
- JSON-RPC；
- Session DB、Artifact Path；
- 环境变量和配置文件；
- Action Approval 与外部系统响应。

## 6. 威胁与控制

### TM-01：Prompt Injection 越权

**场景**：仓库文件或 Tool 输出要求模型读取凭据、关闭安全策略或执行高风险命令。

**控制**

- Runtime hard rules 与项目内容分层；
- 项目指令记录来源和 trust；
- 权限由 Runtime Registry 和 Policy 决定，不信任模型自报；
- Secret 默认不进入 Context；
- 高风险 Tool 需要绑定效果的审批；
- Security Eval 使用间接注入语料。

**剩余风险**：用户可能批准具有欺骗性的合法命令；需要可解释审批 UI 和最小效果展示。

### TM-02：路径穿越和符号链接逃逸

**场景**：`../`、绝对路径、symlink、目录重命名或 TOCTOU 使读写越过 Workspace。

**控制**

- 词法路径与 canonical path 双重检查；
- Workspace/External Root 明确建模；
- 写前与打开时重新校验文件身份；
- 使用安全打开/原子替换原语，避免只做字符串前缀判断；
- 审批绑定 Workspace Revision；
- 构造 symlink/rename race 测试。

**剩余风险**：跨平台文件系统语义不同；Host 后端无法提供容器级隔离。

### TM-03：Shell 注入与进程逃逸

**场景**：字符串拼接、命令替换、后台任务或孙进程绕过超时和取消。

**控制**

- 结构化 argv 优先，显式 Shell 模式单独标识；
- 进程组/Job Object 级取消；
- stdin 默认关闭；
- 超时、CPU/内存和输出上限；
- Turn 完成前扫描并清理托管进程；
- 不用命令文本分类替代 Sandbox。

**剩余风险**：Host 模式仍继承用户权限；高风险命令应使用 Container。

### TM-04：网络数据外泄

**场景**：命令、依赖安装或恶意测试通过 DNS、IPv6、代理或重定向发送源码和 Secret。

**控制**

- Tool/Sandbox Network Profile 默认最小权限；
- 同时覆盖 DNS、IPv4/IPv6、代理环境和重定向；
- Secret 最小化注入；
- Provider 与 Tool 网络能力分离；
- 记录目标摘要，不记录认证内容；
- 禁网集成测试。

**剩余风险**：Host 模式的应用层 allowlist 难以防止所有旁路。

### TM-05：Secret 泄漏

**场景**：环境变量、配置、错误响应或 stdout 进入 Prompt、Session、日志、Trace、Diff 或 Artifact。

**控制**

- Secret Reference，不在领域对象保存明文；
- 每个 Tool 声明所需 Secret；
- 写入任意持久层前统一 Redactor；
- 日志默认记录摘要而非 Payload；
- Artifact ACL、保留期和删除；
- Canary Secret 全链路扫描。

**剩余风险**：未知编码、压缩文件和模型推断可能绕过模式脱敏。

### TM-06：Approval Bait-and-switch

**场景**：UI 展示的命令、文件或目标与最终执行对象不同，或批准后 Workspace/Tool 已变化。

**控制**

- 展示和执行共享同一规范化 Effect；
- 指纹绑定 Tool 版本、参数、cwd、环境摘要、Workspace Revision、Policy 和 Sandbox；
- 任一字段变化使批准失效；
- Approval Request/Response 持久化；
- Client 只能响应 Request，不能自行生成批准事实。

**剩余风险**：复杂 Shell 的真实效果难以完全摘要，应倾向更强 Sandbox。

### TM-07：恶意 MCP、Skill、Hook 或 Tool

**场景**：扩展隐藏能力、动态改变 Schema、绕过 Permission 或窃取 Context。

**控制**

- 来源、版本、签名/校验和与 trust 状态；
- 未信任项目 Hook 默认禁用；
- Tool Schema/版本变化使授权失效；
- 扩展只通过受限 Context 和 Tool API；
- 所有效果经过统一 Permission/Sandbox；
- 输出大小、类型和内容校验。

**剩余风险**：进程内第三方 Python 扩展可拥有过大权限；首版应优先进程外协议。

### TM-08：Provider 流伪造或异常

**场景**：重复 Tool ID、乱序事件、无限 Delta、非法 JSON 或伪造 Usage 导致执行错误和资源耗尽。

**控制**

- Provider Adapter 状态机和 Schema 校验；
- ID 唯一、事件顺序和大小限制；
- 完整 Tool 参数通过 JSON/Schema 后才生成 ToolCall；
- Provider raw payload 不进入公共协议；
- 非法序列映射为 invalid_provider_output；
- Fuzz 分块和乱序测试。

**剩余风险**：供应商语义变化需要及时升级 Adapter Contract Test。

### TM-09：Client 协议伪造与资源耗尽

**场景**：客户端跳过 initialize、伪造 ToolResult/Event、发送巨大 JSON 或制造慢消费者。

**控制**

- 标准 JSON-RPC Schema 和状态检查；
- 只暴露命令，不接受客户端写内部事实；
- 消息、深度、队列和速率上限；
- requestId 幂等和冲突检测；
- 有界队列、Delta 合并和慢客户端断开；
- WebSocket 上线前增加鉴权与 Origin。

**剩余风险**：stdio 默认继承父进程信任，不能防御已控制同一进程树的攻击者。

### TM-10：Session/Event 篡改或回放

**场景**：本地进程修改 SQLite、插入旧审批或制造 sequence 缺口。

**控制**

- event_id 与 Thread sequence 唯一；
- Event + Projection 原子事务；
- Payload/Projection Hash 和一致性检查；
- Approval 绑定不可变 Effect；
- DB 文件最小权限与备份；
- 启动时 integrity 和 migration 检查。

**剩余风险**：同用户恶意进程仍可直接修改本地文件；未来可选事件签名。

### TM-11：重复或未知外部副作用

**场景**：请求已被外部系统提交，本地在保存 Result 前崩溃，恢复后重复执行。

**控制**

- Tool Call 在效果前持久化；
- 稳定 action_id 和业务幂等键；
- 未分类外部写错误进入 UNKNOWN；
- Reconcile 只观察，不执行原请求；
- 无对账能力的非幂等写不自动重试；
- 故障注入覆盖请求前、提交后、结果前。

**剩余风险**：外部系统没有幂等/查询能力时只能要求人工确认。

### TM-12：Sandbox 静默降级

**场景**：容器、系统 Sandbox 或策略加载失败后命令自动转为 Host 执行。

**控制**

- Backend 能力在执行前声明并校验；
- 不满足 Profile 时 fail closed；
- 降级必须由显式用户策略批准，并写入事件；
- UI 和日志展示实际 Backend；
- CI 模拟 Sandbox 不可用。

**剩余风险**：用户可主动选择 Host；产品必须准确描述其安全级别。

### TM-13：供应链与更新

**场景**：依赖、安装脚本、Provider SDK 或发布包被替换。

**控制**

- Lockfile、Hash、最小依赖和依赖扫描；
- 发布构建可复现并生成 SBOM；
- 插件版本固定；
- 更新包签名；
- CI Secret 与发布权限分离。

**剩余风险**：上游合法版本也可能包含漏洞，需要持续更新策略。

## 7. 安全不变量

1. 模型不能授予自己权限；
2. Approval 不能跨 Effect 指纹复用；
3. Sandbox 不可用不静默转 Host；
4. Workspace 边界在实际打开/执行时再次检查；
5. Secret 明文不进入 AgentEvent、日志和 Trace；
6. Cancel 后不启动新 Tool；
7. UNKNOWN 不自动重放；
8. Tool/MCP/Hook 不能绕过统一 Registry；
9. 客户端不能直接写内部 Event；
10. 所有安全降级都有持久事实和用户可见状态。

## 8. 发布门禁

0.7 安全执行版本发布前必须：

- 路径、symlink、进程树、禁网和 Secret Canary 测试全部通过；
- 所有内置 Tool 声明 Effect、Permission、Sandbox 和 Secret；
- 高风险 Tool 的 Approval 指纹属性测试通过；
- Sandbox 不可用测试证明 fail closed；
- 外部写故障注入中重复效果数为 0；
- Threat Model 根据实现更新为 v2；
- 文档准确区分 Host 限制与 Container 保证。

## 9. 后续工作

- 0.5：为 Patch/Shell 补专项威胁分析；
- 0.7：根据真实执行后端更新 Threat Model v2；
- 0.8：为 MCP、Hooks 和 WebSocket 增加独立边界；
- 0.9：引入自动化红队 Eval、依赖扫描和发布 SBOM；
- 1.0：完成安装更新、安全响应和数据删除策略。
