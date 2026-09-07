# ADR 0056：Workspace、Git、环境Source与乐观一致性

- 状态：Accepted
- 日期：2026-09-07
- 范围：0.6.2b
- 前置：ADR 0023、ADR 0043、ADR 0054、ADR 0055

## 1. 背景

0.6.2a建立了可取消Source端口、受控项目指令发现和每模型步骤freshness，但模型仍缺少工作区概览、当前Git状态和少量宿主环境事实。若每类来源独立读取后直接拼接，请求可能混合不同Workspace能力根或明显漂移的外部状态；若直接枚举进程环境或裸执行Git，又会扩大Secret泄漏与仓库配置执行风险。

文件系统、Git索引和进程环境没有共同事务。0.6.2b必须给出可证明的有限一致性，而不是宣称不存在的原子快照。

## 2. 决策

### 2.1 WorkspaceContextSource

Source由宿主绑定规范根、相对工作目录、deny-path、每目录条目上限和正文上限。一次观测只列出根与工作目录的一级可见条目，二者相同时只列一次。每次列举复用`Workspace`和`list_files`，并以相同revision再次核对；不递归、不读取文件正文、不跟随链接。

模型文档`workspace/layout.json`采用确定性JSON，包含相对工作目录、目录路径、条目名称/类型和显式截断标记。默认每目录64项、正文12 KiB。正文越界时只从稳定排序末尾移除展示项，并保留`truncated=true`；底层目录扫描越过10000项或2 MiB名称预算仍失败关闭，不把不完整扫描伪装成正常截断。

### 2.2 GitContextSource

Git Source只能复用宿主绑定的`GitReadRuntime`，禁止直接启动Shell或接受模型提供的命令、路径、环境和配置。模型文档`git/status.json`仅包含：

- `repository`；
- branch、HEAD、upstream、ahead、behind；
- 状态条目的相对路径、原路径、类型、index/worktree状态与submodule标记；
- `total_entries`和`truncated`。

默认请求100项、正文16 KiB。非Git目录成功返回`repository=false`文档；detached HEAD的branch为`null`；初始无提交仓库的HEAD为`null`。不采集远端URL、用户、日志、任意配置或diff。

Git执行保留既有五秒超时、有界stdout/stderr、固定绝对可执行文件、空全局配置、关闭系统配置/Hook/fsmonitor/外部diff、禁止交互/分页和固定最小环境。Workspace scope在Git调用前后核对；绑定变化、超时和暂时I/O错误不能产生部分上下文。

### 2.3 EnvironmentContextSource

环境Source构造时必须接收宿主显式allowlist和值映射。实现只按allowlist逐项取值，禁止遍历映射或自动读取`os.environ`。名称必须符合受限环境键格式，最多32项；包含token、secret、password、passwd、key、credential、cookie、auth、authorization、private、jwt或session语义片段的名称在构造时拒绝。

值必须是UTF-8字符串，不含NUL和控制字符，单值最多1024字节。文档`environment/runtime.json`最多4 KiB，只包含POSIX/平台标识、Workspace相对工作目录和当次存在的allowlist值。值按External trust处理，不能改变Policy或权限。

### 2.4 乐观双观测

注册一个Source时保持0.6.2a行为：单次观测并生成`ContextInspection v2`。注册两个及以上Source时，`SourcedContextEngine`执行固定两轮顺序观测：

1. 第一轮按注册顺序观测全部Source；
2. 第二轮以相同顺序重新观测全部Source；
3. 每轮所有`workspace_scope`必须相同；
4. 同一Source两轮`source_revision`必须相同；
5. revision相同时完整`ContextSourceObservation`也必须逐字段相同；
6. 使用第二轮观测生成Fragment和快照。

来源revision漂移返回可重试`context_sources_changed`。相同revision却改变正文或文档元数据属于Source契约错误，返回不可重试`context_source_invalid`。scope不一致返回不可重试`context_source_workspace_mismatch`。任何错误都停止规划，不提交Context事实，不调用Provider。

该策略只表示两轮观测窗口内未检测到变化，不提供文件系统/Git事务原子性，也不授权后续写入。工具执行仍必须使用自己的revision和准入契约。

### 2.5 持久化与版本

新增`ContextConsistencySnapshot v1`，字段为算法版本、固定两轮、来源数量和共同Workspace scope。新增`ContextInspection v3`，在v2来源快照基础上增加一致性快照，并要求至少两个Source、身份唯一、Fragment绑定一致、全部Source scope等于共同scope。

Agent Event/Thread升级至v12；v3检查记录只能进入v12事件。Session migration 14只推进最低reader标记，不改表、不改写旧Event或投影。v1-v11事件、v1-v2 Context Inspection和既有Source Schema保持冻结。

### 2.6 可观测性

继续使用现有Context Span和`harnessix.agent.context.sources`计数器，只输出固定kind/status。新增一致性指标仅允许固定strategy/result标签，不输出source ID、路径、变量名、正文、revision或scope。失败沿用受控`ContextSourceError.code/retryable`，不记录原始Git stderr和环境值。

## 3. 取舍

### 3.1 一次并发读取

拒绝。并发缩短时间窗口，但不能证明各来源属于同一状态，也不能检测读取期间的来源漂移。

### 3.2 文件系统或Git锁

拒绝。没有覆盖Workspace、Git索引和环境值的共同锁；强行加锁会干扰用户工具并仍不能覆盖外部进程。

### 3.3 启动时缓存

拒绝。Coding Agent会在每步工具执行后改变文件和Git状态，启动快照很快过时。每模型步骤刷新与持久revision更适合恢复审计。

### 3.4 任意环境变量透传

拒绝。黑名单无法穷举Secret名称；宿主allowlist和名称拒绝规则必须同时存在。

### 3.5 自行解析`.git`

拒绝。worktree、submodule和攻击者可控指针具有复杂安全语义，且会形成第二套Git边界。现阶段严格复用已验收Git只读运行时。

## 4. 兼容与回退

- 单Source调用仍生成v2检查记录，保持0.6.2a序列化和一次读取语义；
- 多Source调用生成v3检查记录和v12事件；
- 静态Context继续生成v1检查记录，但当前新事件统一写v12；
- migration 14应用后旧wheel必须以`schema_too_new`拒绝；回退需要恢复一致备份，禁止删除迁移标记或手工下调投影版本；
- 0.6.2b不改变Provider v3、Action、Tool、Artifact、Patch或Process契约。

## 5. 验收门禁

- Workspace根/工作目录排序、deny-path、链接、扫描超限、正文截断和revision竞态；
- Git普通/干净/脏/非仓库/detached/初始仓库、状态截断、超时、取消和运行时绑定失败；
- 环境allowlist、Secret名称、非法值、缺失值、单值/总量边界和步骤间刷新；
- 多Source scope错配、轮次间revision漂移、同revision正文漂移和Provider发网前失败；
- v3检查不含正文、Event v12、migration 14、SQLite重开、Replay、旧reader拒绝和历史Schema冻结；
- Telemetry不泄漏路径、变量名、正文、revision或scope；
- 真实临时Git仓库和非Git工作区验证；
- 全仓回归、严格异步/警告、Schema连续生成、构建、仓库外独立wheel及远端四矩阵CI全部通过。
