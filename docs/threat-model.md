# Harnessix Code 威胁模型 v2

- 状态：当前安全基线，已同步DOC-1.4 API、Product Config、MCP、Skill、Hook与Smoke现行设计
- 更新日期：2026-09-12
- 适用范围：本地优先 CLI、Headless App Server、Agent Runtime、Coding Tools、Session Store、Action Plane

实施说明：当前Kernel已实现单宿主锁、事件CAS/幂等、可信工具准入、输出边界、保守恢复、持久审批检查点、数据库文件权限、结构化存储错误、受管Patch/Process、Context来源控制和Runtime遥测字段隔离。0.7.1新增绑定文件内容、环境摘要、Secret版本、Policy和能力证据的完整Execution Plan；0.7.2新增固定摘要Container Profile/Command、实际后端探测、网络快照与受管出口、Secret Provider、流式Redactor和最终Guard；0.7.3以独立owner、POSIX Session/PTY、Windows Job/ConPTY、HMAC回执和有界持久输出替换单进程生命周期假设，并增加ContainerExecution/ProcessLaunch绑定、spawn前网络复核及标签化残留清理，六矩阵门禁已经通过。0.8.4和0.8.5分别把MCP及Skill/Hook接入受限`ExtensionActionPort`；0.8.6增加严格产品配置、安全读取/迁移、版本化Provider Secret引用、活动配置CAS和零响应暴露Fallback。上述新边界仍未接管全部0.5既有Tool，网络主体认证和发行物信任仍在0.9实施。目标控制与当前保证必须分开解读，参见[0.8设计](m08-product-runtime-and-extensions.md)和[0.7设计](m07-trusted-execution-and-delivery.md)。

Windows已进入1.0正式目标。0.7.1已经完成Windows原生Workspace句柄端口，0.7.2的Sandbox/Secret合同已在Windows CI运行；Windows strong Sandbox优先使用受管Docker Desktop或WSL2容器后端。0.7.3挂起启动、不可breakaway Job、ConPTY及Container统一生命周期、0.7.4受管Git worktree/checkpoint/commit和0.7.5平台中立Action入口均已通过综合门禁；普通Windows目录事务写仍失败关闭。0.7统一入口约束新Tool和未来扩展，0.5既有Patch/Process桥接继续由原不可变批准与专用账本治理，不改写历史事件；0.8已完成本地Agent Protocol与Provider配置产品接线，发行物尚未完成。在[ADR 0063](adr/0063-windows-v1-platform-support.md)规定的完整产品门禁完成前，Windows仍不属于当前产品支持范围。

## 1. 安全目标

Harnessix Code 必须保证：

1. 未授权的模型、项目内容和扩展不能直接获得文件、进程、网络或 Secret 能力；
2. 用户批准的内容与实际执行效果一致；
3. Workspace 外读写有明确策略；
4. 取消、超时和崩溃不留下不可解释的进程或副作用；
5. 未知外部写不会被自动重复；
6. Prompt、日志、Trace、Session 和 Artifact 不泄漏凭据；
7. 客户端不能伪造 Runtime 事实；
8. Sandbox 不可用时不静默降级；
9. Windows 盘符、UNC、ADS、Reparse Point、Junction 和大小写语义不能绕过 Workspace；
10. Commit、Push、外部 Action 和扩展执行必须分别取得与实际效果一致的授权；
11. 多文件发布任意崩溃点都能恢复为可核对状态，不将顺序写误称为原子事务；
12. requested 与 effective Sandbox/Network 能力一致并有运行证据。

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
- 在 0.7 实现完整 DLP、远端多租户和企业 KMS；
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
- 项目指令记录来源和 trust；0.6.1固定Runtime > User > Project > Workspace > Git > Environment，正文不能自报更高优先级；
- 0.6.1使用结构化JSON编码Fragment，检查记录只持久来源元数据、选择结果与正文指纹，不重复保存正文；
- 0.6.2a把项目文件固定为Project trust，只在宿主绑定Workspace祖先链通过no-follow、单硬链接、revision和总量边界读取；失败或竞态在Provider请求前关闭；
- 0.6.2b把Workspace/Git/环境固定为External trust；Workspace只给有界一级概览，Git复用关闭Hook/配置扩展的固定只读运行时并再次过滤deny-path，环境只读取宿主allowlist且拒绝Secret类名称；
- 多Source执行两轮乐观观测并持久一致性算法/scope，不一致在发网前失败；该证据不替代工具revision、审批或效果核对；
- 权限由 Runtime Registry 和 Policy 决定，不信任模型自报；
- Secret 默认不进入 Context；
- 高风险 Tool 需要绑定效果的审批；
- Security Eval 使用间接注入语料。

**剩余风险**：结构化编码和受控发现不能保证模型拒绝恶意正文；0.7.2的Redactor/Container端口尚未在0.7.5接管全部Tool输出，环境名称过滤也不能判断被错误命名的敏感值；双观测不提供跨来源事务原子性；当前只发现活动工作目录祖先链规则，不会在编辑任意子目录文件前自动加载该子树规则。用户仍可能批准具有欺骗性的合法命令，需要可解释审批UI和最小效果展示。

### TM-02：路径穿越和符号链接逃逸

**场景**：`../`、绝对路径、symlink、目录重命名或 TOCTOU 使读写越过 Workspace。

**控制**

- 词法路径与 canonical path 双重检查；
- Workspace/External Root 明确建模；
- 写前与打开时重新校验文件身份；
- 使用安全打开/原子替换原语，避免只做字符串前缀判断；
- POSIX与Windows采用独立平台端口；Windows额外校验盘符、UNC、保留名、ADS、大小写折叠、长路径和Reparse Point/Junction；
- 审批绑定 Workspace Revision；
- 构造symlink/reparse/rename/file-sharing race测试。

**剩余风险**：跨平台文件系统和第三方过滤驱动语义不同；Windows普通目录尚无抗Reparse Point竞态的事务写端口，受管Git候选仍需远端矩阵关闭；Host后端无法提供容器级隔离。

### TM-02A：Windows 命名与 Reparse 逃逸

**场景**：模型使用盘符相对路径、UNC/设备命名空间、保留名、尾随点/空格、ADS 或 Junction/Reparse Point，使展示路径与内核对象不一致。

**控制**

- 模型只提交平台中立相对 `WorkspacePath`，不能提交盘符、UNC 或设备前缀；
- Windows 平台端口逐段拒绝 ADS、保留名、尾随点/空格和 Reparse Point；
- 使用句柄身份和最终路径复核，不用 `Path.resolve()` 或大小写字符串前缀作为安全边界；
- 打开、审批后复核和写入分别覆盖检查后替换竞态；
- Windows CI 创建真实 Junction/Reparse、大小写和长路径夹具。

**剩余风险**：第三方文件系统、云盘过滤驱动和网络共享可能具有不同语义；未通过能力探测时只允许 guarded 读或失败关闭。

### TM-03：Shell 注入与进程逃逸

**场景**：字符串拼接、命令替换、后台任务或孙进程绕过超时和取消。

**控制**

- 结构化 argv 优先，显式 Shell 模式单独标识；
- POSIX进程组与Windows Job Object分别建立进程树归属、取消和宿主退出核对；
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
- Product Config只保存Secret引用；Process/Artifact等已接入路径按各自合同使用Redactor或最终Guard；
- 日志默认记录摘要而非 Payload；
- Artifact ACL、保留期和删除；
- Canary Secret 全链路扫描。

**剩余风险**：Action Plane当前在敏感键守卫前先持久化完整Action Request，被拒绝的疑似明文仍进入
Journal和失败Snapshot；未知编码、压缩文件和模型推断也可能绕过模式脱敏。完整现行边界见
[API模块设计](modules/api.md#25-敏感数据真实流向)。

### TM-05A：Secret 生命周期和派生值泄漏

**场景**：Secret 明文虽未进入 Tool 参数，却通过子进程环境、错误回显、编码/分片输出、Git Diff、诊断包或派生 Token 泄漏。

**控制**

- 领域对象只保存版本化 `SecretRef`，仅在 spawn 边界解析；
- 批准绑定 Secret 名称/版本摘要，不绑定或展示明文；
- 子进程只得到计划声明的 Secret，清空未声明环境和代理凭据；
- stdout/stderr、异常、Artifact、日志、Trace、模型 Context 和诊断包共享流式 Redactor；
- 使用跨 chunk、Base64/URL 编码和短前后缀 Canary 测试；
- Redactor 失败时阻止输出发布，但效果状态仍按真实进程/Action 记账。

**剩余风险**：任意加密、压缩、哈希推断和被允许进程主动外传无法由字符串 Redactor 完全阻止，仍依赖最小注入和网络隔离。

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

- 来源、版本、内容/定义摘要与Trust Grant状态；
- 非Bundled Hook没有精确定义授权时失败关闭；
- Tool Schema/版本变化使授权失效；
- Skill只作为不受信内容经渐进读取Action加载，不解释其中的权限或可执行声明；
- Hook输入只获得Thread/Turn/Action身份及参数/结果摘要；
- 扩展只通过受限 Context 和 Tool API；
- 所有效果经过统一 Permission/Sandbox；
- MCP/Skill执行输出大小、类型和内容边界；Hook执行严格结果结构和调用方已知Secret精确值检查，原始输出资源预算仍待补齐。

**剩余风险**：Hook Grant当前不是密码学签名，不绑定Workspace/Tenant且只在Registry捕获时检查有效期；Bundled是宿主标签而非发行签名证明；Definition与Port实际来源没有交叉校验；Hook输出Guard晚于底层Action成功结算；恢复没有Owner Lease或Action账本对账。本地同UID主体还可替换用户可写Skill/Hook定义或调试宿主。完整风险、崩溃窗口和关闭条件见[Hook模块设计](modules/hooks.md)与[Skill模块设计](modules/skills.md)；发行物签名、远端分发和多用户隔离仍待后续关闭。

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

### TM-08A：Provider配置投毒与不安全Fallback

**场景**：配置通过重复键、链接替换、Secret错版或伪造Profile链切换到攻击者端点；Provider在
已返回文本或Tool Call后失败，透明Fallback造成重复输出、重复副作用或错误计费归因。

**控制**

- 最大256 KiB严格JSON，拒绝未知字段、重复键、非有限数、类型转换、过深结构和悬空引用；
- 配置文件使用POSIX目录描述符/`O_NOFOLLOW`或Windows句柄/Reparse Point安全读取，并核对读取
  前后身份；产品入口额外拒绝位于Workspace内的配置文件；
- Provider、Profile、精确模型、能力要求、Fallback顺序和Secret名称/版本形成不可变摘要；
- 诊断与Provider构造重新生成并核对完整`ProfileSelection`，不接受调用方截短或重排；
- 活动指针以旧配置摘要和旧Profile联合CAS切换，配置事件和Fallback决策分别形成连续Hash链；
- 自动Fallback只接受零响应暴露的三类可重试失败，且先持久化决策；任何响应、文本、Tool Call、
  完成事件或未来未知Provider事件均关闭Fallback窗口；
- Adapter局部尝试号重写为步骤内连续全局序号，失败尝试及已知用量仍进入Session；
- 构造或CAS失败时关闭全部已创建Client，不开放stdio。

**剩余风险**：Environment Secret版本由部署者声明，不能证明云端真实版本；Python SDK仍持有
不可变Key字符串直到Client关闭；SQLite Hash链不能抵御可同时篡改程序与数据库的同UID主体；
已发送但未收到任何响应的模型请求可能已经计费，Fallback只保证没有向Agent暴露输出，不保证
供应商侧零费用。

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

### TM-09A：Action HTTP身份伪造、跨租户访问与资源耗尽

**场景**：网络调用方伪造`Principal`中的Tenant、Subject、Roles或Approval Actor，按已知UUID读取其他
Tenant的Action/Event，批准或对账不属于自己的Action；超大Body、深层JSON、全量Event响应、并发Inline执行或
慢连接耗尽API、Journal和Executor。

**当前控制**

- 默认本机命令监听Loopback，部署文档要求公网前置受信Gateway；
- Domain模型限制部分标识长度、拒绝额外顶层字段，Journal以Tenant和幂等键约束重复写；
- Action状态、审批和Reconcile由Journal期望状态及Lease守卫，HTTP不能直接指定目标状态；
- HTTP Metric不记录Path UUID、Arguments或Header正文；
- 当前Docker以非root用户运行。

**剩余风险**：API没有Authentication、可信Principal注入、Tenant授权、行级读取过滤、Security Scheme、Body/JSON/
Response预算、分页、限流、并发上限或Deadline。任意可达主体可声明任意Tenant并读取、批准、对账或执行；Docker默认
绑定`0.0.0.0`会放大误发布风险。外层Gateway只能临时限制网络暴露，不能修复Journal在敏感键守卫前保存完整Request
的内部缺口。该边界是0.9.4身份/数据安全和0.9.3容量可靠性的发布阻塞项，详见
[API模块设计](modules/api.md)。

### TM-09B：Framework Adapter身份混淆、重复Action与结果过度暴露

**场景**：共享LangChain Tool实例固定了一个Principal和Action Context，调用方又可覆盖Metadata中的`adapter`标签；
Framework重试或Checkpoint恢复时因Tool Call ID没有持久绑定Action ID而新建Action；完整Action Snapshot被作为正常
Tool Content写入模型历史或外部Callback，且`pending_approval`、`failed`、`unknown`等状态仍呈现为Framework Tool成功。

**当前控制**

- Adapter只构造Action Request并调用Client，不直接操作Journal、Policy或Executor；
- Action Plane继续校验Tool、Effect Hint、Policy、审批、幂等键和状态转换；
- 写Tool可要求租户内业务幂等键，`UNKNOWN`不能由Action Service自动重执行；
- `SecretRef`不包含明文值，Domain模型限制部分身份字段长度；
- `langchain-core`是可选依赖，不影响基础包导入。

**剩余风险**：当前无可信动态Principal、Tool Call ID到Action ID绑定、真实LangGraph Checkpoint/Interrupt恢复、
类型化Action状态投影、模型可见字段白名单、响应预算、Callback脱敏或Adapter Trace关联。Context Metadata是浅可变字典，
其中同名`adapter`值覆盖默认来源；完整Request、Principal、Secret Ref标识、Approval和Result可进入模型历史。多租户、写Action
或启用外部Tracing前必须完成Adapter v2身份、恢复和最小输出设计，详见[Adapter模块设计](modules/adapters.md)。

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

### TM-12A：能力声明高于实际后端

**场景**：配置请求强隔离或选择性网络，但执行器缺少 namespace、代理、Seatbelt Profile、Docker 能力或 Windows 后端，仍以较弱模式启动。

**控制**

- Capability Probe 在计划前生成证据，执行前再次校验；
- `ExecutionPlan` 同时绑定 requested/effective level、后端和版本；
- 后端缺失、版本变化或策略无法完整落实时在 spawn 前失败关闭；
- 降级必须由新的用户意图、新计划和新批准表达；
- 发布文档只声明 CI 和真实 smoke 已证明的平台/后端组合。

**剩余风险**：后端自身实现缺陷仍需供应链固定、版本升级测试和攻击回归发现。

### TM-13：供应链与更新

**场景**：依赖、安装脚本、Provider SDK 或发布包被替换。

**控制**

- Lockfile、Hash、最小依赖和依赖扫描；
- 发布构建可复现并生成 SBOM；
- 校验第三方许可证、版权通知、贡献来源与发布物内源代码许可声明；
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
11. Windows 进程在恢复运行前必须已属于预期 Job Object；绑定失败不得退化为仅杀根 PID；
12. Commit 与 Push 是不同效果；Push 默认关闭且不能继承 Commit 批准；
13. 多文件 transaction 每个成员 replace 前先持久化意图，replace 后先记录可核对事实再推进游标；
14. Secret 明文不得进入 ExecutionPlan canonical JSON、fingerprint 输入、Session Event 或审计载荷；
15. Capability requested/effective 不一致时不得执行。
16. Provider自动Fallback不得跨越响应或Tool Call暴露边界，且没有持久审计不得切换。

## 8. 发布门禁

0.8产品运行时版本发布前必须：

- 路径、symlink、进程树、禁网和 Secret Canary 测试全部通过；
- 所有内置 Tool 声明 Effect、Permission、Sandbox 和 Secret；
- 高风险 Tool 的 Approval 指纹属性测试通过；
- Sandbox 不可用测试证明 fail closed；
- 外部写故障注入中重复效果数为 0；
- Threat Model 根据实现更新为 v2；
- 文档准确区分 Host 限制与 Container 保证；
- macOS、Linux和Windows分别通过路径、进程树、取消、恢复和Sandbox能力门禁；Windows平台中立CI不得替代原生执行测试。
- Product Config严格解析、Secret canary、迁移崩溃、活动CAS、Hash链篡改和Fallback暴露边界测试通过。

## 9. 后续工作

- 0.5：Patch、Process、交付和Tool调度专项威胁分析已随纵向切片补齐；
- 0.7：根据真实执行后端更新 Threat Model v2；
- 0.8：Agent Protocol、MCP、Skills、Hooks及Provider产品配置威胁边界已完成；
- 0.9：引入自动化红队 Eval、依赖扫描和发布 SBOM；
- 1.0：完成安装更新、安全响应和数据删除策略。

## DOC-1.4 Smoke现行风险补充

- **门禁与目标身份分离**：`allow_network is True`及`--allow-network`只决定是否创建Provider，不限制目标主机、端口、DNS结果、私网地址、地域或组织。语法合法的任意HTTPS端点均可通过当前Config v1。
- **配置对象替换**：Smoke配置读取使用`O_RDONLY | O_NONBLOCK`和打开后的普通文件检查，但没有`O_NOFOLLOW`、Owner、Mode、Hardlink或父目录验证；符号链接和`0644`文件当前均可被接受。
- **凭据路由**：配置可同时指定`base_url`和任意格式合法的`api_key_env`。若不可信主体替换配置，可把所选环境变量中的值发送到非预期端点。配置不保存Key不能单独关闭该风险。
- **当前控制**：默认不联网、严格JSON、16 KiB文件上限、禁自定义Header、禁代理环境、禁重定向、HTTPS、零重试、固定场景、传输预算和白名单Report降低误用及泄露面，但不能替代安全配置打开、端点Allowlist、Secret Scope与受管Egress。
- **消费与证据**：Token和尝试上限不是人民币金额硬预算；正常退出删除临时Session，Report也不自动持久化或签名。失败Usage不完整时必须保持消费未知并通过供应商账单核对。
- **合同歧义**：Report v1只对`reason=passed`实施完整跨字段校验，非通过Reason可构造自相矛盾字段组合。消费方必须先按Reason拒绝所有非通过报告，后续合同应使用判别联合或完整矩阵校验。

完整设计、受控探针、风险优先级和关闭条件见[Smoke模块设计](modules/smoke.md)。

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

可复查证据见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md) 与 [测试记录](testing-and-evals-milestone-history.md#22-053b2b-kernel-受管写闭环验收2026-09-04)。

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

## 0.5.4b2b2a Agent进程投影补充（2026-09-05）

- Action Approval仍是唯一执行许可。Session中的Process审批决定只能由受信投影函数从已核对ActionSnapshot生成；普通Session ApprovalRecord、计划摘要或Reducer通过本身都不授予Executor能力。
- Agent v9把READY/LEASED/RUNNING/RECONCILING保持为WAITING_ACTION。只有绑定同一计划、Action ID/指纹和结果摘要的终止观察才可产生Tool Result；Reducer拒绝状态倒退、重复终止、结论错配和跨调用证据。
- Process计划、审批和状态Item是Session私有事实，模型历史仍只映射完成的消息、ToolCall和白名单ToolResult字段。完整argv仍存在原ToolCall/ActionRequest；摘要不是内容脱敏，也不是抗同UID恶意篡改的签名。
- Runtime本片只在重启时保留WAITING_ACTION，不执行、轮询、取消或回收Action。宿主硬退出、逃逸后代、Process Artifact正文、Sandbox和跨账本自动恢复仍是b2c/0.7边界。
- migration10仅阻止旧reader接管，不改变数据库隔离。真实`e0e8498` v8 wheel升级、旧reader拒绝和提交前后硬退出已通过；这只证明SQLite迁移事务与历史字节兼容，不证明防同UID篡改、硬件断电或跨账本原子性。

## 0.5.4b2c1 Process Agent运行时补充（2026-09-05）

- `ProcessAgentBridge`是宿主受信能力，不是模型可构造的通用执行器。只有显式注入才广告`host.process`；工具必须为HIGH/NON_IDEMPOTENT_WRITE、强制审批/幂等且禁止自动对账。桥接拒绝`auto_execute=True`，审批答复内不执行命令。
- Action Journal ApprovalRecord仍是唯一许可。Session决定只由已核对快照按真实决定时间投影；外部决定晚于Turn截止也必须留存事实，但不刷新预算。相同决定补投影不重复状态转换，不同actor/outcome/reason冲突。摘要、Reducer通过或Session数据库可写性都不能直接授权Executor。
- `resume_turn`每次至多读取一次Action。活跃状态保持WAITING_ACTION，相同快照不追加；终态效果、结果摘要和离开等待同批提交。UNKNOWN/MANUAL_INTERVENTION停止模型循环，不回READY、不重放、不按持久PID/PGID发信号。
- 模型公开结果省略PID和Base64正文，只显示生命周期及双流字节数/SHA256/truncated/EOF。摘要不是脱敏：字节数、退出码和哈希仍可能泄露侧信道；完整argv继续持久在ToolCall/ActionRequest，禁止承载凭据。b2c2现只通过受作用域保护的Artifact引用提供已捕获正文，不直接注入模型历史。
- b2c1没有解决Action提交后Session审批请求前的硬退出、WAITING_ACTION取消、跨进程并发决定及完整租约/终态提交矩阵。没有外部监督器时Agent/Worker宿主死亡后子进程仍可能存活；当前能力不是Shell、容器、OS Sandbox、网络隔离或同UID防篡改边界。

## 0.5.4b2c2 Process Artifact补充（2026-09-05）

- `ProcessObservation.process`和发布器属于宿主受信内存边界，不进入模型工具参数。发布器只接受已核对终态、原Process批准及当前未结算Call；Artifact引用不能创建、批准、执行或重放Action。
- 正文是规范JSONL和Base64原始字节，不做文本替换或隐式解码。12 KiB分片、24 KiB页和1 MiB总上限控制资源消耗；无法保存全部已捕获前缀时不发布，不能二次截断后伪造`complete`。这不限制Effect Journal中Action结果本身的敏感度。
- `process_output`行与Tool Result引用/终态Session事件同事务。提交前失败不留孤儿正文，提交后确认丢失不重复插入；配额或普通发布故障仅省略展示引用。Effect Journal与Session仍为两个事务资源，Action终态不能被Artifact事务回滚。
- reader同时验证manifest/body、Thread/Turn/Call、Process批准、Action ID/指纹、双流摘要、规范Base64和连续offset；用途改写、正文/引用/摘要篡改返回`artifact_corrupt`。这些校验面向意外损坏和宿主错绑，不抵抗能同时重写数据库与摘要的同UID攻击者。
- 读取要求原Thread和工作区scope，过期后正文清空并保留tombstone。scope不是身份令牌，SQLite未加密；日志可能包含源码、测试数据或秘密，上层API仍需认证、导出授权、备份保护和保留策略。当前没有通用DLP或内容级Redactor。
- migration11只扩展Artifact用途白名单；迁移原子与旧reader拒绝不等于硬件断电、恶意数据库修改或网络多租户隔离。更完整跨库恢复、WAITING_ACTION取消和SDK闭环现由b2c3补齐；0.7仍负责Sandbox与外部监督。

## 0.5.4b2c3 跨库恢复与取消补充（2026-09-06）

- Runtime只在持久ToolCall明确为`host.process`、NON_IDEMPOTENT_WRITE且缺少Session审批时进入恢复分支；缺少原专用端口时保留事实，不降级到通用工具执行。稳定Action ID/幂等键用于防重复，不是授权令牌或加密签名。
- Action已决定但Session请求/决定缺失时，只能从匹配Action快照补投影。跨调用、Thread/Turn、工作区、Principal、工具版本、宿主绑定或请求指纹漂移继续fail closed；两个数据库可同时被恶意修改时这些摘要不能提供防篡改保证。
- WAITING取消仅终止Session观察并保守记为unknown。它不撤销PENDING/READY/RUNNING Action、不回滚副作用、不终止进程，也不构成“用户取消命令”的证明；UI和API不得误报。
- RUNNING/RECONCILING租约过期同事务保存`UNKNOWN/lease_expired`，避免终态无结果导致投影失败。UNKNOWN不得自动重试，但仍不能证明进程停止；外部监督和孤儿进程回收继续属于0.7。
- 跨进程审批由数据库事务裁决，相同决定幂等、不同决定冲突。该规则防止应用层重复授权事件，不替代API认证、RBAC、审计actor真实性或多租户隔离。
- 双SDK测试证明当前历史白名单不发送Action ID、私有Process效果、绑定指纹、幂等键和未读取的Base64正文。模型主动调用`read_artifact`后会获得授权页内容；这是显式数据流，不是DLP，Artifact仍可能包含源码或秘密。

## 0.5.4c Git与测试Profile补充（2026-09-06）

- Git能力默认不存在，只有受信宿主提供绝对可执行文件才注册。模型不能选择cwd、仓库、子命令、revision、pathspec或config；工具版本绑定程序/cwd身份、固定环境和资源策略。摘要用于漂移检测，不抵抗同UID同时替换文件和篡改进程内事实。
- 每次Git读取都要求`rev-parse --show-toplevel`严格等于授权根，防止子目录借用父仓库越过工作区意图。固定环境/参数关闭用户和系统配置、分页器、交互、可选锁、Hook、fsmonitor、external diff与textconv；恶意仓库仍可能用超大索引/对象消耗有限资源，因此保留5秒、捕获和结果上限。
- `git_status`只有结构化条目上限；底层输出截断时整体失败。`git_diff`只返回48 KiB完整UTF-8前缀及观察摘要；达到更高的进程输出停止阈值时失败，不把部分观察伪装成完整差异。Git工具不执行提交、checkout、clean、merge、push或任意alias。
- `run_tests`公开输入只有Profile名称。程序、argv、工作区和超时由宿主固定，Profile全集与后端Process绑定进入公开工具版本。额外字段、类型强转、未知名称及绑定漂移在Action启动前拒绝；工作区错配直接结束Turn，模型无法用名称拼接命令参数。
- 测试并非只读：解析后的固定命令仍创建`host.process` Action，由Action Journal唯一批准并由外部Worker执行。Session中的`run_tests`审批投影、公开Schema或`passed`结果均不能替代Action许可；每次观察重新核对公开调用和后端Action完整身份。
- 非零退出仅表示已确定测试失败，`passed=false`允许修复循环；启动、清理和输出证据不完整仍是failed/unknown。将二者混淆会导致错误自动重试或掩盖孤儿进程风险，因此状态、退出码与测试结论保持独立。
- Profile完整argv会进入Effect Journal，不得包含凭据。Process Artifact可能包含源码、断言数据或秘密；模型显式读取后会得到正文。当前没有DLP、加密、容器、网络出口控制、CPU/内存强配额或同UID防篡改。
- 私有受管副本不是自动Git worktree。测试示例中的Git初始化是受信宿主夹具，不意味着任意副本可提交/合并或自动交付源目录。0.5.4c闭环也不是非示例缺陷集Eval或生产容量证明。

完整命令、错误、恢复与替代方案见[ADR 0043](adr/0043-git-and-controlled-test-feedback.md)。

## 0.5.6 Tool并发补充（2026-09-07）

- 并发资格只来自受信`ToolDescriptor`且默认关闭；只有`READ_ONLY`可显式开启。模型参数、提示词或工具名称不能授予并发能力，非法写声明在注册前失败。
- Kernel只合并同一响应中连续、无需审批、调用/定义Effect一致且指纹匹配的安全读取。审批、Patch、Process、未知和未声明工具都是屏障，后续读取不能越过；因此并发不会扩大写权限或改变批准对象。
- Kernel和Coding Tool分别使用1—16的有界批次/信号量，默认4。该限制控制单进程任务、线程和FD压力，不限制同UID其他进程，也不是跨主机Workspace锁。
- 并发完成顺序不写入Session；结果仍按Provider顺序校验和提交，避免竞态改变Replay。任一异常、Turn取消、父Task取消或Runtime关闭都会取消并排空未完成任务后再结算。
- 新字段进入完整工具指纹。旧终态历史可读；旧未完成调用不能静默切换调度语义，必须`tool_contract_changed`失败关闭。滚动升级应先排空在途Turn。
- 工具错误类别统一只影响诊断聚合，不改变稳定错误码或授权语义。`process_interrupted`、审批、冲突、存储和取消仍按更具体规则分类，避免`process_*`前缀覆盖UNKNOWN/中断事实。
- 剩余风险包括第三方只读工具错误声明、CPU密集读取争抢线程池、同UID进程并发改写Workspace及跨进程TOCTOU。生产宿主只应给经过并发测试的内置工具opt-in；0.7必须提供跨进程锁、Sandbox和资源配额。

完整证据和取舍见[调度专项研究](research/tool-scheduling-and-errors.md)与[ADR 0053](adr/0053-tool-concurrency-and-error-taxonomy.md)。

## 0.7.5统一Action Plane补充（2026-09-09）

- **调用方伪造风险**：`CodingActionInvocation`没有effect、risk、policy、Sandbox或executor字段；这些事实来自宿主自摘要`TrustedToolBinding`。额外字段、Tool版本/指纹/Schema摘要或注册Binding不一致均在执行能力发放前拒绝。
- **资源漏报**：资源由宿主resolver生成，并与Workspace Snapshot、Sandbox网络模式和Secret版本绑定交叉检查。该机制依赖受信resolver正确实现；任意第三方Python resolver不能与宿主同进程加载，扩展只能提交到预注册端口。
- **扩展越权**：`ExtensionActionPort`固定其实际source/source id，只暴露该来源Binding和Plan生命周期。MCP/Skill/Hook/custom不能取得Host Executor、Session Store、Secret Provider或Workspace对象，也不能读取其他来源Plan。Hook装配仍需单独核对Definition来源与Port实际来源；现行Runtime缺少该比较，错误Mapping键可形成审计身份混淆。0.8.4/0.8.5已分别约束MCP进程和不可执行Skill/声明式Hook；远端分发、发行签名与供应链清单仍由后续里程碑关闭。
- **Hook提权**：Hook来源不具有覆盖宿主deny的能力；任何Hook请求仍按同一Binding、资源和Policy规划。Hook Definition或返回值不是批准记录。现行`READ_ONLY`是宿主Binding声明而非Executor副作用的机械证明，第三方逻辑必须进入独立Sandbox/MCP/Container Action。
- **审批错绑与漂移**：Approval Checkpoint只绑定Plan fingerprint。执行重开时同时核对Route/Execution Plan、当前Tool Binding、Workspace Snapshot和持久批准；参数、cwd、环境、策略、Sandbox、Secret版本、文件或Git配置变化使批准失效。
- **审计泄漏与篡改**：append-only Action Audit事件仅保存资源、输出和Artifact摘要，不保存输出或文件正文；私有Route Plan为执行/恢复保留规范化调用参数，可能包含路径和命令元数据，但疑似凭据字段被拒绝且Secret值只能通过独立Provider注入。因此Plan/Audit数据库仍按敏感状态保护，不能对外直接发布。当前投影、不可变payload、冗余索引和事件链不一致时失败关闭。字段名检测不是通用DLP，无法识别被放入普通文本字段的任意凭据；上游Context与Tool Schema仍必须禁止Secret正文进入参数。SQLite和SHA-256不能抵抗可同时改写数据库与程序的同UID恶意主体，也不提供不可抵赖签名。
- **外部写重复执行**：Git Push投影到稳定外部Action id和Effect Journal；调用开始后的任何异常为unknown。恢复只执行`ls-remote`对账，不再次Push。目标OID、旧OID和第三种OID分别收敛为成功、失败和人工处置。
- **直接旁路**：旧ActionService中的Git Push Tool额外要求对应Route已获批准且当前处于running，plan/action/intent/idempotency任一错绑即deny。直接调用`_GitRunner`仍属于受信宿主代码能力；不受信扩展不能获得该对象，未来插件进程必须继续依靠OS Sandbox。
- **Git协议与凭据**：remote URL拒绝内嵌凭据、query、fragment、HTTP、自定义协议和歧义路径；Git固定环境关闭prompt、外部配置、Hook、replace refs和attributes，并只开放显式协议。0.7不装配公网凭据，不能把本地bare remote验收解释为远端认证、known-hosts或Secret防泄漏完成。
- **跨库窗口**：Execution Plan先于Route持久化，崩溃可留下不可达孤立Plan；没有Route、Approval和当前Binding时不能执行。Route进入running后宿主硬退出，重开只转unknown。该设计不承诺跨SQLite事务原子性，而以不可达和保守恢复保证安全。

对应攻击和硬退出证据见[0.7.5测试记录](testing-and-evals-milestone-history.md#72-075统一action-plane与git-push发布候选验收2026-09-09)、[专项研究](research/unified-action-plane-and-extension-boundaries.md)和[详细设计](m07-trusted-execution-and-delivery.md#14-075-action-plane与安全验收详细设计)。

## 0.8.3 双向交互与Pull-Live补充（2026-09-09）

- **响应身份混淆**：stdio请求可以乱序完成。SDK只允许唯一Reader读取stdout并以JSON-RPC `id`结算待决Future；重复未决ID、未知Response ID、非法Response、EOF和读取失败会稳定失败全部待决请求，不能把下一帧误配给先返回的协程。
- **长轮询队头阻塞与资源耗尽**：READY阶段以协商的`maxPendingRequests`限制并发任务，所有出站帧仍经过唯一有界Writer。`events/next`最长30秒，关闭先唤醒长轮询；攻击者不能用无限等待占满无界Task。该边界是单客户端本地进程保护，不是远程DoS防护。
- **Delta丢失或伪完整**：实时文本只保存在每Thread最多1000条的内存缓冲中，不作为恢复事实；溢出必须返回`liveGap`。客户端发现缺口后不得继续拼接为完整回答，只能等待持久完成Item。服务重启会丢失全部Delta，但不能丢失Session事件。
- **提问伪造、重放和错配**：Question ID及Request/Answer/Result Item ID由原Tool Call稳定推导。回答只接受当前Thread、Turn、Question的`WAITING_INPUT`边界；相同回答幂等，不同回答冲突，取消、关闭或过期后的回答失败。审计Answer不直接进入Provider历史，模型只接收配对Tool Result。
- **Steering投递到错误Turn**：命令必须绑定预期活动Turn；终态、finalizing或取消中拒绝。Steering只在模型步骤边界进入下一次Context，不取消当前Provider请求；Reducer强制当前响应及其Tool配对排在新用户输入之前，避免并发到达改变模型历史。
- **Artifact跨Workspace读取**：客户端不能提交Scope。`ScopedProtocolArtifactReader`从Thread恢复Workspace并重新获取当前能力，再校验Session归属、用途、TTL、摘要和页界限；宿主未装配Reader时方法不广告。SQLite路径、私有计划和完整Session对象不进入公共协议。
- **重复交互提示**：CLI本进程记录已成功提交的Approval/Question ID，跨进程则从持久Answer或Approval事实恢复。客户端崩溃发生在命令提交后、响应前时必须重用原领域`requestId`，不能创建新决定；最终副作用仍由既有Approval和Action幂等边界保护。
- **剩余风险**：同UID主体仍可终止或调试本地stdio进程、篡改可写数据库或替换宿主程序；0.8.3不提供网络认证、二进制签名或多用户隔离。完整发行签名和诊断包治理属于0.9，远程协议必须另建认证和主体绑定后才能开放。

对应详细设计与回归见[ADR 0072](adr/0072-durable-interaction-and-pull-live-stream.md)、[0.8详细设计](m08-product-runtime-and-extensions.md#6-083-薄cli与双向交互详细设计)和[0.8.3测试记录](testing-and-evals-milestone-history.md#75-083-薄cli与双向交互验收2026-09-09)。

## 0.8.4 MCP补充（2026-09-09）

- **新增不受信主体**：第三方MCP Server二进制、协议实现、显示身份、Capability、Tool名称、Description、Annotation、输入/输出Schema、分页Cursor和调用结果；这些数据都不能成为本地Permission或风险事实。
- **隔离边界**：生产本地Server只能由固定镜像摘要、`ContainerExecutionSpec`和`ContainerSandboxProfile`构造stdio进程；默认禁网，选择性网络必须走受管出口。MCP进程不获得Session、Action Audit、Execution Plan、Secret Store或宿主Docker Socket。
- **目录投毒与TOCTOU**：完整Tool定义和目录形成内容摘要；调用锁内绕过缓存重新获取所有分页，重复Cursor、重复名称、超限、非法Schema或任一摘要变化均在`tools/call`前失败并持久化`schema_changed`。
- **Schema资源消耗**：JSON Schema仅接受2020-12 object根；外部引用、基址重定义和无界正则关键字失败关闭，Schema、参数和结果分别限制字节、深度、节点及字符串长度。无法解析的本地引用转换为稳定参数错误，不向上泄漏解析器异常。
- **权限提升**：Description和Annotation只作低信任显示；Effect、Risk、资源、Recovery和Sandbox只能来自宿主`McpTrustedToolPolicy`。模型只看到当前目录中同时存在且已注册到对应`ExtensionActionPort`的Tool。
- **不确定副作用**：调用发送后的超时、断连、异常结果、`input_required`和写Tool `isError`均不能证明未产生效果；写Action进入UNKNOWN，只允许宿主显式外部对账，不自动重放。
- **Secret泄漏**：目标环境只由Execution Plan绑定的Secret短期解析，值不进入argv、目录、连接事件或配置；Tool结果在进入Action输出、日志或模型Context前执行同一Secret Guard脱敏。
- **生命周期**：连接状态和目录代次持久化；宿主重开不接管历史连接。SDK关闭后通过容器名、进程ID和执行摘要标签证明无残留，无法证明时以`mcp_process_cleanup_failed`失败。当前关闭先置内存关闭标志，清理期间取消不被普通异常分支捕获，可能导致后续关闭无法重试，是0.9.3必须关闭的高优先级风险。
- **Server反向暴露**：可选MCP Server只通过本地stdio导出显式低风险只读Action；列表和调用都重核绑定，远端客户端无权批准写操作。0.8.4不监听网络，不实现OAuth或任意Header。
- **剩余风险**：默认产品尚未装配MCP；同UID主体仍可替换宿主配置、重算无密钥Hash链或调试本地进程；Container Runtime本身属于高权限TCB。Catalog没有总字节预算，MCP/Execution Plan/Action Audit三库没有跨库事务和统一关联；In-process信任及Reconciler只观察约束依赖宿主装配。模型Provider的0.8.6 Secret引用不适用于MCP；Streamable HTTP、受管OAuth和远端目标身份由0.9.4补齐，发行签名与SBOM由0.9关闭。

对应现行设计、源码依据和回归见[MCP模块设计](modules/mcp.md)、[ADR 0073](adr/0073-mcp-catalog-binding-and-sandbox.md)、[MCP运行时与安全源码研究](research/mcp-runtime-and-security.md)及[0.8详细设计](m08-product-runtime-and-extensions.md#7-084-mcp详细设计)。

## 0.8.5 Skills与Hooks补充（2026-09-09）

- **内容包提示注入**：Skill正文和资源始终是不受信模型输入；目录只保存元数据和摘要，Frontmatter中的Tool、Hook、Shell、模型、Secret或权限声明不产生能力。
- **名称劫持**：同一来源重复限定名称全部失效；跨来源同名要求显式`source/name`，禁止按来源优先级静默覆盖。
- **路径越界与TOCTOU**：Root通过POSIX目录描述符或Windows句柄/Reparse Point检查固定身份；每次读取重核Root、普通文件、链接数、目录观察和内容摘要。符号链接、Junction、特殊文件、敏感路径、回退段和嵌套Skill资源失败关闭；POSIX硬链接当前会出现在资源列表，但实际读取由Reader拒绝。
- **资源耗尽**：Manifest发现限制总目录、总条目、深度和Skill数；资源枚举当前只有深度、单目录条目和最终文件数上限，缺少累计目录/条目、Deadline和可取消预算，超宽纯目录树仍可阻塞同步调用。
- **正文持久泄漏**：Skill Store只保存目录、Manifest和访问摘要，不保存绝对Root、正文或资源；调用方提供受保护明文时，正文跨越Action边界前执行Secret Guard。保护集合默认为空，未知或变形Secret不在当前保证内。
- **访问审计**：敏感路径、Catalog缺失、名称冲突和期望Manifest不匹配等早期拒绝当前不写Skill Access Event；Registry在Secret Guard前先记录成功，Guard拒绝时Skill与Action账本结论不同，三库也没有统一关联或事务。
- **目录生命周期**：Catalog和Manifest摘要阻止旧计划静默读取新内容，但同一Router不支持Skill Definition注销或原子替换；目录更新必须停止旧计划并重建生命周期，默认产品尚未实现该装配。
- **可执行扩展旁路**：Hook不支持任意Shell、HTTP、Prompt或进程内回调，只能调用宿主注册的低风险只读`source="hook"` Action；第三方需要执行能力时必须使用MCP/Container和目标Action自身的Policy/Sandbox。
- **定义替换**：非Bundled Hook授权绑定完整定义摘要；事件、Matcher、顺序、超时或Action版本/指纹变化都会使旧Trust Grant失效。运行前再次核对Registry和Binding。
- **信息过度暴露**：Hook处理器只看到身份和摘要；来源身份再次哈希，原始Action参数、结果、路径、模型正文、环境和Secret均不传递。
- **拒绝绕过与权限提升**：`before_action`只能Blocking/Fail Closed并按确定顺序执行；任一拒绝或失败阻断目标调用。其他Hook只能Advisory/Record Only，非法`deny`记录失败，`allow`不能覆盖目标Action的deny、审批或Sandbox。
- **挂起、取消与重放**：每个Hook有独立超时并取消底层Action；调用取消持久结算两层状态。重开把遗留Running收敛为Interrupted，重复Dispatch只返回既有终态，不自动重放。
- **剩余风险**：0.8.5不提供远端Skill安装、签名Marketplace或可执行Hook生态；默认产品尚未装配Skill，上下文信任标签和提示注入真实模型门禁未完成。同步扫描/YAML/SQLite不可及时取消，Store路径/WAL权限、分页、保留和迁移仍需加固；同UID配置篡改可重算无密钥Hash链，不能抵御宿主账户失陷。发行签名和供应链清单属于0.9。

对应现行Skill设计、源码依据和回归见[Skill模块设计](modules/skills.md)、[ADR 0074](adr/0074-skill-snapshot-and-hook-action-boundary.md)、[Skills、Hooks与供应链边界源码研究](research/skills-hooks-and-supply-chain.md)及[0.8详细设计](m08-product-runtime-and-extensions.md#8-085-skills与hooks详细设计)。

## 0.8.6 Provider与配置产品化补充（2026-09-09）

- **配置来源投毒**：只接受受信宿主显式路径下的单一严格JSON文件；不支持include、远端URL、
  命令替换、环境正文插值或插件解析器。文件读取复用跨平台安全句柄并拒绝链接、特殊文件、
  多硬链接及观测漂移。
- **配置混淆**：Provider定义、模型Profile和Secret Source分离；所有ID排序唯一，引用、版本、
  能力、Fallback图和累计尝试数在构造网络Client前全量验证。规范摘要与原文件摘要分离，分别
  用于语义身份和源迁移CAS。
- **Secret污染与泄漏**：配置和数据库只保存Secret名称、版本和环境变量定位；只解析选中链的
  白名单值且一个环境变量只能对应一个Secret身份。Provider构造核对精确版本，将API Key限制
  为8 KiB可打印ASCII，拒绝自定义Header环境变量，诊断和CLI错误不传播值或
  原始异常。可变副本立即清零，但Python/SDK不可变字符串不承诺内存安全擦除。
- **迁移覆盖与半写**：v1到v2要求完整源摘要、私有锁、同目录临时文件、文件/目录fsync、私有
  备份和替换前二次CAS。替换前失败保持v1；替换后退出由v2幂等重开收敛，不根据缺失收据回滚。
- **配置切换竞态**：Provider候选和Runtime组件全部初始化成功后才使用期望旧摘要与旧Profile
  联合CAS发布活动指针；失败会关闭已创建Client，不开放stdio。同配置内Profile切换也不能
  丢失更新；配置热加载关闭，活动Turn不会在流中途换Provider。
- **重复输出与副作用**：Fallback只在零响应暴露、可重试白名单错误和审计成功时发生。尝试和
  用量元数据可以先持久化；响应身份、文本、Tool Call、完成或未来未知事件一经观察即禁止切换。
- **审计篡改**：配置加载/激活/迁移和Fallback使用独立连续Hash链；读取时核对正文、索引、
  前驱和Head；POSIX共享父目录、链接或多硬链接数据库在打开前拒绝。该链是篡改检测，不是
  同UID攻击者下的密码学不可抵赖日志。
- **产品入口隔离**：内置`agent-server`固定单一已解析Workspace，只装配现有只读Coding Tools；
  配置文件必须位于Workspace之外，状态目录不能与Workspace互相包含。stdio不提供网络身份，
  不能桥接为多用户服务。
- **剩余风险**：真实模型能力与端点运营状态需要0.9.6受控Provider门禁持续验证；远端MCP
  OAuth与公网Git凭据分别由0.9.4/0.9.5建立独立目标身份和Secret作用域，不能复用当前模型
  Provider认证。

对应设计、源码依据和回归见[ADR 0075](adr/0075-provider-profile-secret-and-safe-fallback.md)、
[Provider/Profile配置与安全Fallback源码研究](research/provider-profile-config-and-safe-fallback.md)
及[0.8详细设计](m08-product-runtime-and-extensions.md#9-086-provider与配置产品化详细设计)。
