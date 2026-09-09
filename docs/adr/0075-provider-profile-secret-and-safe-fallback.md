# ADR 0075：Provider Profile、Secret 引用与安全 Fallback

- 状态：已接受
- 日期：2026-09-09
- 决策范围：Harnessix Code 0.8.6

## 1. 背景

0.4已经实现双 Provider Adapter、流式失败语义和模型尝试账本；0.6实现终态 Turn Retry 与
显式 Provider 切换；0.8.1～0.8.5形成 Agent Protocol、App Server、CLI 和扩展边界。当前仍
缺少面向最终用户的配置快照、Profile 选择、Secret 引用、诊断、迁移及内置启动装配。

直接让 Adapter 读取任意环境变量无法证明使用了哪个 Secret 版本；在 Provider 流之间简单
重试则可能在文本或 Tool Call 已经暴露后重复输出和副作用。

## 2. 决策

### 2.1 配置格式与来源

采用单文件、UTF-8、最大256 KiB的严格 JSON v2：

- 未知字段、重复键、非有限数、非法UTF-8和过深结构全部拒绝；
- 通过 `SecureWorkspaceReader`复用POSIX目录FD及Windows句柄链，拒绝符号链接、Junction、
  Reparse Point、特殊文件、多个硬链接和读取漂移；
- Provider、Profile和Secret Source使用有序唯一数组，规范摘要不依赖对象插入顺序；
- 内存模型冻结且拒绝额外字段。

产品配置不支持动态include、命令替换、`${...}`正文替换、插件代码或远端配置URL。

### 2.2 Secret

Provider定义只保存`SecretReference(name, version)`。宿主Secret Source保存来源类型和环境
变量定位，不保存值。启动和诊断必须调用`SecretProvider.resolve`并核对精确版本；Provider
API Key还限制为不超过8 KiB的非空可打印ASCII。明文只在
Provider Client构造作用域内解码，可变副本随后清零。SDK内部不可变字符串副本属于Provider
Client生命周期，关闭Client后释放，禁止写入配置、Session、审计、错误或诊断。

禁止继承`OPENAI_CUSTOM_HEADERS`和`ANTHROPIC_CUSTOM_HEADERS`，防止认证跨端点污染。

### 2.3 Profile与能力

Profile固定Provider ID、精确模型、声明能力、要求能力、HTTP边界和显式有序Fallback列表。
加载时验证：

- active/引用对象存在且ID唯一、规范排序，每个环境变量至多定位一个Secret引用；
- 并行工具要求蕴含工具调用能力；
- 每个候选满足首选Profile的要求能力；
- Fallback图无环、展开后不重复，链最多六个Profile；
- Fallback链中各Profile的`max_attempts`之和不超过模型账本32次上限。

不存在模糊模型匹配、默认供应商替换或运行中配置热改。新配置只影响新启动的Provider Bundle；
活动Turn继续绑定原对象。

### 2.4 安全 Fallback

Fallback仅对`transport`、`rate_limit`和`provider_internal`三类可重试失败开放。以下事件属于
模型响应暴露边界：

- `ResponseStarted`；
- 任意文本开始、增量或完成；
- `ToolCallCompleted`；
- `ResponseCompleted`。

`ModelAttemptStarted`、`ModelUsageObserved`和`ModelAttemptFinished`是内部持久计费/审计事实，
不属于模型输出。候选失败后，只有在尚未越过暴露边界、失败可重试、存在下一候选且
Fallback审计已经持久化时，才抑制该候选的`ResponseFailed`并启动下一候选。失败尝试及已知
用量仍完整进入Session。

编排器把各Adapter的局部尝试序号改写为步骤内全局连续序号，并把Provider字段改写为配置中
的精确Provider ID。审计写入失败时不切换，原失败直接返回。

### 2.5 配置持久事实与切换

私有SQLite存储使用WAL、`synchronous=FULL`和0600文件，保存：

- 不含Secret值的不可变配置快照；
- hash-chained配置加载/激活/迁移事件；
- hash-chained Fallback决策。

激活使用期望旧配置摘要与旧Profile组成的CAS。重复激活同一摘要/Profile幂等；其他并发结果返回
`product_config_conflict`。启动必须先完成离线诊断、保存快照和全部Provider构造，再以CAS
激活；构造或CAS失败会关闭已创建Client，不开放stdio或创建Thread。

### 2.6 迁移

只支持已冻结的v1到v2迁移。v1中的`api_key_env`被转换为Environment Secret Source及版本化
引用；多个Provider共用同一旧环境变量时复用同一个Secret引用，避免为同一凭据伪造多个版本
身份。迁移顺序为：安全读取与旧摘要校验→内存转换→v2全量验证→同目录写临时文件并
fsync→发布私有备份→原子替换→目录fsync→记录迁移收据。任一步失败时不发布部分v2；调用方
可从备份恢复。再次迁移v2返回无写入的幂等结果。

### 2.7 产品入口

新增：

- `harnessix config diagnose`：离线输出规范诊断JSON；
- `harnessix config migrate`：要求旧摘要CAS并输出迁移收据；
- `harnessix agent-server`：按配置/Profile装配Provider、固定Workspace只读Coding Tools、
  Session、请求账本和stdio App Server。

产品启动拒绝位于Workspace内的配置文件，以及与Workspace互相包含的状态目录，避免后续写工具
把控制面纳入模型可修改范围。

完整写工具、TUI和发行安装器仍按0.9交付；该入口不宣称替代0.9产品体验。

### 2.8 明确不做

0.8.6不开放远端MCP Streamable HTTP/OAuth、自定义Header、公网Git凭据、配置热重载或自动
恢复旧Provider流。此前文档中把远端MCP/Git凭据泛化归入0.8.6的表述被本ADR替代：这些能力
必须分别通过0.9.4安全审查和0.9.5跨平台Dogfooding，未完成前继续失败关闭。

## 3. 备选方案

### 3.1 TOML/JSONC与多层自动合并

未采用。注释与多来源覆盖便于手工维护，但会增加重复键、来源追踪和无损迁移复杂度。0.9配置
向导可以生成严格JSON，而不削弱运行时契约。

### 3.2 把Secret值写入0600认证文件

未采用。权限不能阻止凭据进入备份、同步、崩溃取证和误提交范围。

### 3.3 文本首Token前仍允许Fallback

未采用。`response_started`后Provider已经建立响应身份和潜在计费，且后续异常可能隐含未完成
Tool Call；保守边界更易审计和恢复。

### 3.4 同时实现远端MCP OAuth与Git SSH

未采用。二者的认证生命周期、网络身份和恢复语义与模型Provider不同；仓促复用通用Header
会破坏0.7受管出口和Secret边界。

## 4. 后果

正向后果：模型选择可复现，Secret版本可核对，配置变化可审计；Fallback不会跨越模型输出或
Tool Call边界；用户获得可直接启动的stdio服务入口。

代价：配置暂不支持注释和热重载；每次切换需要重启Provider Bundle；远端MCP与公网Git仍
保持关闭；Environment Secret Source只能证明宿主声明版本，不能替代云Secret Manager自身
的版本证明。

## 5. 验收条件

1. v1/v2、重复键、未知字段、路径攻击、大小/深度、迁移崩溃与CAS冲突测试通过；
2. 缺失/错版Secret、缺依赖、能力不足、Fallback环和尝试上限诊断测试通过；
3. Fallback在零暴露失败时切换，任意响应或Tool Call暴露后绝不切换；尝试序号连续且审计可
   重放；
4. OpenAI-compatible和Anthropic真实Adapter通过注入Secret构造的离线Mock传输回归；
5. `config diagnose/migrate`与`agent-server`装配测试通过，输出不含Secret canary；
6. macOS/Linux/Windows CI以及全量Ruff、Mypy、Schema和pytest门禁通过。
