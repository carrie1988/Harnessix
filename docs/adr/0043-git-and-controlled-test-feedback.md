# ADR 0043：Git只读反馈与宿主测试Profile

- 状态：已接受并实现
- 日期：2026-09-06
- 范围：0.5.4c
- 依赖：ADR 0023、0025、0030、0038—0042

## 1. 背景

0.5.3已经允许模型在私有受管副本内提出并经审批执行Patch，0.5.4a/b已经把固定宿主进程接入Action Plane，并建立Action Journal唯一审批、独立Worker、WAITING_ACTION、Process Artifact和保守恢复。此前仍缺少两个完成编码反馈循环的能力：

1. 修改前后读取真实Git状态和差异；
2. 运行宿主认可的测试，而不把任意命令、路径或参数交给模型。

直接向模型暴露`host.process`会让模型选择argv，能力面过宽；把测试伪装为只读工具则会绕过命令执行审批和副作用账本。调用系统`git`也不能默认信任仓库或用户配置，因为外部Diff、textconv、fsmonitor、分页器和Hook都可能启动额外程序。

## 2. 决策

### 2.1 Git能力是显式启用的固定只读端口

宿主只有在构造`CodingToolRuntime(..., git_executable=<绝对路径>)`时才注册：

- `git_status`：读取分支、HEAD、upstream、ahead/behind和结构化变更条目；
- `git_diff`：显式读取`worktree`或`staged`差异。

模型不能提交仓库路径、revision、pathspec、Git配置或任意子命令。运行时固定：

```text
git --no-pager --no-optional-locks
    -c color.ui=false
    -c core.fsmonitor=false
    -c core.hooksPath=/dev/null
    <固定子命令与固定选项>
```

`git_diff`额外固定`--no-ext-diff --no-textconv`，并以`--`结束选项。进程环境不继承父进程，只允许宿主绑定的最小变量；关闭用户全局配置、系统配置、可选锁、交互提示和分页器。运行前执行固定`rev-parse --show-toplevel`，结果必须严格等于规范工作区根；位于父仓库中的子目录不会被当作授权仓库。

这不是对所有Git命令“无副作用”的证明，而是对当前固定命令、选项、环境和程序身份的窄声明。Git工具仍使用现有只读工具串行锁、Turn取消和5秒时限。

### 2.2 Git公开契约严格且有界

`git_status`输入只有：

```json
{"limit": 100}
```

`limit`范围为1—200。输出条目区分`ordinary`、`renamed`、`unmerged`和`untracked`，分别保存index/worktree状态；重命名必须同时给出原路径。`total_entries`统计完整解析结果，`truncated`明确表示返回前缀，`revision`是本次原始porcelain输出SHA-256。原始状态输出超过进程捕获上限时失败，不返回不完整结构。

`git_diff`输入只有：

```json
{"target": "worktree", "context_lines": 3}
```

`target`仅允许`worktree|staged`，上下文行范围0—20。输出文本为不超过48 KiB的完整UTF-8前缀，同时给出前缀字节数、已观察字节数、已观察流SHA-256和`truncated`。非法UTF-8明确失败，不用替换字符改变证据。底层进程仍有有限捕获和总输出停止阈值；达到停止阈值时返回工具失败，而不是假装取得完整Diff。

Git可执行文件身份、工作区身份、固定环境、固定选项和资源预算进入工具版本。重启后配置变化会形成新ToolDescriptor，旧调用不能静默迁移。

### 2.3 `run_tests`只接受宿主预注册Profile名称

公开模型契约为：

```json
{"profile": "unit"}
```

模型不能提供`program`、`arguments`、`cwd`、环境、超时或代码。宿主使用`TestProfile`注册：

- 唯一且排序的名称；
- 面向模型的简短说明；
- `HostProcessRuntime`中已经允许的程序别名；
- 固定argv；
- 不超过宿主上限的固定超时。

最多注册32个Profile。名称和程序别名最多64字符，argv最多128项、合计不超过64 KiB且禁止NUL。构造`RunTestsAgentBridge`时即核对Profile、规范工作区、`ProcessActionExecutor`真实绑定、程序允许表和时限；任何漂移都在创建模型工具前拒绝。

公开`run_tests`版本绑定以下事实：

- `run-tests/v1`策略；
- 后端`host.process`完整ToolDescriptor；
- 规范工作区；
- 全部Profile定义；
- 公开输入Schema。

因此修改测试命令、程序、工作区、资源预算或Action绑定都会改变版本，不能复用旧批准。

### 2.4 测试执行继续使用原Process Action Saga

`RunTestsAgentBridge`是`ProcessAgentBridge`的受限前端，不增加测试专用执行器、审批表或队列：

```text
模型 run_tests(profile)
  → 校验公开ToolCall与工作区
  → 宿主解析Profile为固定ProcessRequest
  → 确定性创建唯一 host.process Action
  → Action Journal记录完整固定命令并请求审批
  → Session投影同一审批，进入WAITING_APPROVAL
  → 批准只写Action唯一决定，进入WAITING_ACTION
  → 独立ActionWorker执行
  → resume_turn核对Action快照
  → 可选Process Artifact归档
  → ToolResult返回profile、passed和进程摘要
```

Session中的公开调用仍是`run_tests`，Effect Journal中的执行事实仍是`host.process`。每次准备、决定、同步和观察都重新解析同一Profile，并逐项核对公开ToolCall、后端Action请求、Principal、Action ID、幂等键、工具版本、工作区和参数摘要。Session数据或Action快照任一被篡改都不能形成执行许可。

### 2.5 测试失败不是运行时失败

当进程完整退出且双流/回收证据确定时，Action生命周期为`SUCCEEDED`，即使测试退出码非零。公开结果增加：

```json
{"profile": "unit", "passed": false, "returncode": 1}
```

`passed`仅在`stop_reason == "exited" && returncode == 0`时为真。这样模型可以读取失败日志、修改代码并再次选择Profile。启动失败、超时、取消、清理失败或证据不完整仍按既有Process语义返回failed/unknown，不能冒充测试断言失败。

未知Profile、额外字段和类型强转在Action创建前形成有界失败ToolResult，允许模型修正调用；不会创建Effect Journal记录。工作区错配不是模型可修正参数，按宿主集成错误fail closed；由于非幂等ToolCall已经持久化而Runtime不把异常冒充确定ToolResult，Turn保守以`uncertain_effect`中断，但Effect Journal仍为零Action。其他契约/身份错误同样不自动降级为模型可处理错误。

## 3. 完整反馈闭环

当前离线验收把已有能力组合为一个Turn：

```text
run_tests失败
  → read_artifact读取stderr
  → read_file取得内容与revision
  → apply_patch请求审批并修改私有副本
  → run_tests再次审批并通过
  → git_status核对变更集合
  → git_diff核对实际内容
  → 模型提交最终回答
```

两次测试是两个独立Action和两次独立审批；第一次批准不会授权第二次执行。Patch仍由受管副本账本批准和执行。Git读取不能授予Patch或Process权限。三个事实域各自保持原权威来源，不增加“超级事务”：

- Session Store：Agent对话、等待状态和投影；
- Effect Journal：命令意图、Action批准、租约、执行结果；
- Patch副本账本：文件计划、批准、写意图和效果。

跨库恢复继续依赖稳定身份与只读核对，不自动重放非幂等操作。

## 4. 安全边界

### 已提供

- Git和测试能力均由宿主显式启用；
- 模型无法选择Git路径/子命令/config，也无法选择测试argv；
- Git禁用已知可启动外部程序的配置路径；
- 测试命令必须经过Action Policy/Approval、独立Worker和持久结果；
- 固定程序文件身份、cwd身份、环境和预算绑定版本；
- 输出和参数有界，取消/关闭等待真实资源回收；
- 模型wire不包含固定测试argv、Action ID、幂等键、审批指纹或Process Base64正文。

### 未提供

- `HostProcessRuntime`不是OS Sandbox；测试代码仍拥有宿主进程权限；
- 未实现容器、网络隔离、CPU/内存/文件数配额或DLP；
- 不提供任意Shell、PTY、后台任务、Git提交/合并/推送；
- 私有受管副本不是自动Git worktree；示例仅由受信宿主在副本中初始化测试仓库；
- 不自动把副本修改合回源目录；
- 不保证清理脱离进程组的后代或宿主硬退出后的孤儿；
- Git固定命令的边界不扩展到未来Git子命令，新增命令必须单独威胁建模和测试；
- 本闭环是确定性功能验收，不等于真实缺陷集上的Coding Eval或C端容量验证。

## 5. 错误与运维语义

| 场景 | 结果 |
|---|---|
| 工作区不是精确Git根 | `tool_not_found`或`tool_path_denied` |
| Git超时/进程或输出证据异常 | `tool_timeout`或`tool_io_failed` |
| Git输出非法UTF-8/超过结构化上限 | `tool_invalid_utf8`或`tool_limit_exceeded` |
| Profile不存在/公开参数非法 | `test_profile_not_found`/`tool_invalid_arguments`，不创建Action |
| Profile与宿主程序、工作区或预算不匹配 | 构造失败、不广告工具；Thread工作区错配则零Action并保守中断Turn |
| 测试断言失败且进程完整退出 | Tool成功、`passed=false` |
| Worker执行证据不完整 | 既有Process unknown语义，停止自动循环 |
| 等待期间取消或宿主硬退出 | 沿用ADR 0042；不撤销许可、不自动重放 |

部署必须把Profile和Git可执行路径视为代码化宿主配置，经过评审并记录Git提交/wheel摘要。Profile不得携带凭据；当前Process Action会持久化完整argv。生产运行不应把不可信仓库测试直接放在无隔离宿主上。

## 6. 取舍与替代方案

### 方案A：模型直接调用`host.process`

拒绝作为默认测试接口。虽然复用现有执行链最少，但模型可以组合被允许程序的任意argv，审批人员难以基于稳定语义授权，也无法在工具Schema中表达组织认可的测试集合。

### 方案B：把`run_tests`实现为普通只读Tool

拒绝。测试会执行仓库代码并可能写文件、联网或启动子进程，不能因“意图是验证”而归类为READ_ONLY。

### 方案C：自行解析`.git`文件而不调用Git

拒绝。工作树/索引/rename/upstream语义复杂，重复实现容易错误。选择固定系统Git并收紧配置、命令和输出边界。

### 方案D：本片直接增加容器Sandbox与任意Shell

延后。当前目标是以已有Action/Process Saga形成最小生产可信反馈面；容器隔离、资源治理和孤儿监督属于后续0.7，不以未验证抽象扩大变更面。

## 7. 验收

- Git：显式启用、结构化rename/untracked、staged/worktree分离、UTF-8截断/full digest、外部Diff/fsmonitor不执行、父仓库拒绝和非仓库有界失败；
- 测试：公开Schema只有Profile、固定argv只进入Action、批准前不执行、Worker一次执行、非零退出映射`passed=false`、未知Profile/参数注入不创建Action、配置漂移拒绝；
- 组合：脚本Provider完成失败→日志→Patch→通过→Git反馈，源目录不变；
- SDK：OpenAI和Anthropic官方SDK通过离线HTTP完成`run_tests → git_status → git_diff → 回答`，wire不泄漏私有命令和Action证据；
- 示例：`python -m examples.coding_feedback`无需API Key或远程中间件。

本ADR完成0.5.4c当前定义的Git和受控测试反馈，不表示任意Shell、OS隔离、源目录交付或0.5.5真实Coding Eval已经完成。
