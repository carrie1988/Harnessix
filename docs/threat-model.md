# Harnessix Code 威胁模型 v1

- 状态：0.2 架构基线
- 日期：2026-09-02
- 适用范围：本地优先 CLI、Headless App Server、Agent Runtime、Coding Tools、Session Store、Action Plane

0.3 实施说明：当前 Kernel 已实现单宿主锁、事件 CAS/幂等、可信只读工具准入、输出边界、保守恢复、持久审批检查点、数据库文件权限、结构化存储错误和 Kernel 遥测字段隔离。审批绑定当前工具契约/参数/Workspace 路径，但不提供 OS 隔离、actor 身份认证、文件内容或环境完整性保证。真实 Sandbox、网络隔离、完整 Secret Redactor 和 MCP/Hook 仍未实现；本威胁模型中的目标控制不能全部视为当前保证，参见 [Kernel 支持边界](m03-runtime-kernel.md)。

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

## 0.5.2 实施补充（2026-09-03）

- 已实现 Workspace 根对象/拒绝策略绑定、FD/no-follow 读取、有界搜索和 Kernel 注入的真实调用归属；不从模型参数采信 Thread/Turn 身份。
- 已实现私有 Session 内有界 JSONL Artifact：载荷绑定实际发布器，发布重新检查活跃宿主/调用/审批；正文、manifest 和结果同事务提交，跨 Thread/重绑定 Workspace 不能读取。
- SHA/长度/记录数及结果关联检测损坏；清理保留 tombstone，保护活跃 Thread，不把过期、缺失或损坏伪装成空页。逻辑配额不是整个磁盘的硬隔离。
- 宿主 Python 端口、数据库文件和同进程工具仍在受信边界内；scope/UUID 不是网络身份凭据。普通代码中的秘密或恶意指令仍可能进入 Session/Artifact，默认敏感路径拒绝不是通用 DLP。未实现 Secret 全文检测、OS Sandbox、导出授权或写/进程工具。
- 0.5.4b1的进程Action只由宿主显式注册，argv会持久化到Effect Journal，因此禁止携带凭据；SecretRef尚未解析并在启动前拒绝。b2b1新增的Agent/Action稳定身份绑定和快照核对可阻止跨调用、主体、工具版本或宿主绑定复用，但摘要不是签名，受信Python宿主仍在边界内。审批、幂等键和UNKNOWN恢复不构成文件/网络/进程树隔离，宿主硬退出后也不能根据历史PID安全清理。Agent事件/运行时接入、Sandbox和宿主死亡监督仍未实现。

详细实现与可验证边界见 [ADR 0026](adr/0026-transactional-artifacts.md)。

## 0.5.3b2b 受管写实施补充（2026-09-04）

- 只有宿主显式配置专用 Patch 端口才开放模型单文件写；模型仅提交严格提案，不能提供 actor/approved/scope/plan_id。写审批绑定真实调用、提案、副本和持久计划，与只读审批分开。
- 源目录只用于明确选择文件的导入；写入仅发生在持锁受管副本。普通文件/no-follow/根身份/来源及元数据检查沿用后端，不把 hash+rename 说成对源目录并发编辑的 CAS。
- Session 持久消费审批边界后才进入写后端。取消、超时和关闭等待后台线程，已经发生的效果不假报回滚；双账本缺证据时保持 unknown，不自动补写或重放模型。
- 模型 wire 使用结果白名单，私有计划与效果证据不回灌；这不意味着原始提案代码、读取内容或完整 Session 无敏感信息。私有 Python 端口和本地数据库仍属于受信宿主边界，摘要不是签名，actor 不是身份认证。
- 目前仍不是 OS Sandbox、任意进程隔离、网络策略或通用 Secret Redactor；不支持跨文件原子事务、源目录自动合入或安全执行任意仓库代码。多文件/Process 必须分别补充威胁分析。

可复查证据见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md) 与 [测试记录](testing-and-evals.md#22-053b2b-kernel-受管写闭环验收2026-09-04)。

## 0.5.3c1 只读整组计划与 Diff 补充（2026-09-04）

- 多文件提案和整组私有载荷严格绑定路径、顺序、工作区、镜像预算与指纹；沿用单文件路径/类型/编码/漂移拒绝，没有增加写权限。
- 整体复核只是逐项观察，不是原子快照；Diff 展示历史计划，即使来源随后变化也不能被解读成当前磁盘事实或已批准效果。
- Diff 的完整片段摘要与文本预览分别标记，序列化总量有界；截断展示不能作为整组批准对象。预览可能包含用户代码/敏感内容，本片不自动发给模型或发布到 Artifact，不声称 DLP。
- 未持久化整组批准/执行，不增加跨文件回滚、源目录合入、Sandbox 或命令执行。后续 c2/c3 必须覆盖组审批错绑、部分执行及 Artifact 跨归属访问，不能复用 c1 的只读测试代替。

## 0.5.3c2a 整组预留与审批补充（2026-09-04）

- 整组指纹覆盖副本、稳定宿主请求、完整 manifest、有序成员及各自指纹；重复路径/错绑/重排不能复用批准。此请求尚不是受 Kernel 验证的 Thread/Turn/Call，c3 才增加实际调用边界。
- 所有成员在同一 SQLite 事务预留，失败回滚整组；审批与执行分离，组决定不自动批准成员，旧单文件接口拒绝拆分消费。检查同时覆盖归属列与完整组计划，单独清空归属列也不能解锁写入。
- c2a 审批阶段所有组成员仍 pending；后续显式执行入口见 c2b。组记录和决定校验和只提供一致性检测，不能防御同 UID 恶意重写整个数据库或私有 Python 对象；现有锁不是操作系统沙箱。
- 新组共享既有64计划/32 MiB镜像配额，并占用总1 MiB组元数据逻辑预留；每组预留16 KiB决定空间。限制不代表SQLite物理文件/日志或RSS上限，也不预先分配真实磁盘空间。
- v1→v2 仅在副本独占锁、身份及旧数据校验后事务迁移。旧 reader 拒绝新格式；不能通过改 user_version 绕过。实际旧 wheel 与迁移退出矩阵验证不重写源目录/副本目标文件。
- 本片只证明预留/审批事务边界。逐文件替换、部分效果、写后取消与恢复仍归 c2b，不能把本片11个退出切点计作多文件写恢复。

## 0.5.3c2b 顺序写入与部分效果补充（2026-09-04）

- 整组持久开始即消费批准，源码漂移导致零文件修改也不能沿用原组批准重试。每成员写前仍复核；两次检查之间不存在文件系统 CAS，逐文件 rename 不等于整组原子事务。
- 调度只允许成功前缀、至多一个未成功已调度成员、pending 后缀。组批准不会一次性批准整个后缀；旧单文件 execute/reconcile 均拒绝组成员。内部共用方法要求可信宿主锁/组准入，不是防御同 UID 恶意 Python 调用的沙箱。
- 组终止原因和成员效果分开。最后文件写后取消、全部文件已写但组结果未提交时崩溃，均不能回滚或重放来伪装“未发生”。包含未知成员时 unknown 优先；相同后镜像而无临时 inode 归因仍未知。
- 恢复只观察已有成员并追加观察事实，不改变目标文件。已终止的原因不可重写；未终止运行核对后为 interrupted，不自动继续模型。报告是历史效果，不承诺文件以后没有被其他进程修改。
- batch_run_events 每组最多开始/终止两个有界事件，复用单文件事实，不复制过时效果快照。校验缺失开始、错绑、不同决定、越序和版本；校验和不是防恶意重签名。v2→v3 迁移失败不推进版本，旧 reader 拒绝新库。
- 验收包含44个真实退出场景、全部成员位置的取消/超时、fsync 调用及结果记账故障；不将进程退出模拟声称为全部硬件断电覆盖。本片仍无 Kernel 批量模型工具、Shell、源目录合入或自动实际效果 Diff 发布。


## 0.5.3c3a 宿主整组调用绑定补充（2026-09-04）

- 独立调用审批覆盖 Thread/Turn/Call、完整工具/提案指纹和全部后端组计划；只读、单文件、成员或后端组指纹均不能代替外层调用批准。模型输入中任何审批/归属字段均拒绝。
- 这是受信宿主桥接，不是 Session 活跃性凭证或 OS 安全边界。宿主必须先持久批准并消费等待、传播原 Turn 时限；旧 Kernel 不能直接注册桥接；c3b 通过独立组端口接入。默认模型工具范围不扩大。
- 恢复缺少原完整计划或宿主决定时返回 unknown；计划/批准/运行丢失、契约错绑不能靠目标相同字节报告成功。原批准尚未镜像后端时也不自动镜像。恢复不触发保存、批准或执行。
- 公开输出不带原始编辑文本、宿主批准身份、成员 ID 或私有根路径；路径与摘要仍是用户工作区信息，不宣称通用内容脱敏。完整私有证据不进入模型结果或结果 repr。
- Token/Task/超时和重复关闭均等到工作线程排空；取消后仅允许后端已开始效果记账，不调度后缀。5秒操作预算含排队，但不替代跨审批/重开的原 Turn 预算。本节 c3a 不提供 Session 组合恢复或 Diff Artifact；前者已在 c3b 验收，后者仍待 c3c。

## 0.5.3c3b Kernel 整组授权与效果补充（2026-09-04）

- 默认无模型批量写工具；只有宿主显式组端口可用。独立组审批绑定当前调用与完整计划，旧只读/单文件批准、其他副本/调用/顺序不能升级为组授权。
- Session 决定不等于后端执行；持久消费 WAITING 后才能镜像组决定并一次执行。两个账本非原子，关键提交窗口和每成员替换前后均用真实子进程退出验证，未模拟全部断电/硬件故障。
- 私有8 KiB组效果与公开输出分离；公开结果被丢弃仍保留已归因前缀和非正常原因。在线与 Replay 都拒绝错绑效果、伪造恢复和结果顺序；模型 wire 不携带私有计划/ID/批准摘要，不宣称所有公开路径均已脱敏。
- Token/Task/重复取消/Runtime关闭及原截止时间必须排空线程。离线审批不会刷新时限；review 超时不能提交决定。等待缺端口直接保守结算，不在 WAITING 中反复尝试未知工具。
- WAITING 取消、尚未镜像后端决定、原计划/端口/数据缺失或损坏时允许 unknown；不能为改善状态显示而补批、重建或重放。已持久 ToolResult 不重新观察，恢复不把源文件或目标 inode/mtime/ctime 改写。
- migration8 原子推进最低 reader 标记，旧事件/投影原文保留；旧 reader 拒绝新库。单文件后端、组账本v3、OS/同UID边界不变。
- 只读 Artifact 发布器不因批量模型工具而接受写调用。计划展示和历史效果 Diff 的专用准入仍待 c3c；没有 Shell、网络执行或源目录自动合入能力声明。

## 0.5.3c3c1 差异报告边界（2026-09-04）

- 纯文档自洽不构成授权；宿主入口重新绑定真实 Thread/Turn/Call、原完整组计划、批准与精确运行快照。报告方法不读取 Session，不冒充活跃性或持久消费凭证。
- 计划展示不证明当前来源可写。历史仅展示 applied/observed_after 编辑，未知和未执行成员不可隐去，已结算原因不随“全部应用”改写。当前目标变更不覆盖历史归因，未结算运行拒绝报告而非自动观察。
- 预算同时计算 JSON UTF-8 和末尾换行；全部成员说明必须保留。编辑只能按前缀截断，片段保留完整长度/SHA；`complete` 不等于已批准或执行成功。
- 只读取原私有镜像/账本，不调用 prepare/save/reply/execute/reconcile，不写目标或追加事件。取消/超时和反复关闭排空报告线程，真实退出后生成重试不产生新授权或副作用。
- 宿主载荷默认 repr 不输出私有身份或代码，但报告正文含工作区路径、摘要与代码预览，需按源码信息保护；未声称通用脱敏。
- 原只读发布器明确拒绝将这些报告作为写调用 Artifact 发布；当前没有新引用或 Session 事件，事务发布/读取/配额/过期仍待 c3c2。不得将报告准备的退出测试计为未来归档事务已验收。
