# ADR 0045：历史缺陷任务物化与宿主隐藏检查

- 状态：已接受并实现0.5.5b1
- 日期：2026-09-06
- 范围：内置历史任务目录、固定revision私有物化、ready清单、宿主隐藏检查

## 1. 背景与目标

0.5.5a已经定义任务、检查观察、Git证据、评分和报告契约，但尚未证明任务来源是真实历史缺陷，也没有可重复构造缺陷基线或执行隐藏检查。若直接使用当前工作树、可变分支或公开测试文件，评测结果会随仓库变化，且可能把后续修复历史和测试答案暴露给被评Agent。

首个任务来自Harnessix自身已修复的OpenAI-compatible流式工具调用缺陷。修复提交`1c11449de0e817a41b830bb8e45e6e34d3baf009`表明：首个分片已经建立非空工具调用ID后，后续分片可能使用空字符串表示没有新增身份；旧实现把空字符串解释为身份漂移。任务基线固定为该提交父提交`9f24961840fa704e7c7a344c648164d8afe793b7`，只允许修改`src/harnessix/models/_chat_stream.py`。

本片目标是把上述来源证据固化为可重复运行的私有任务工作区，并由宿主执行不会进入模型上下文的行为与回归检查。它不在同一变更中引入模型驱动、Action审批、评分报告编排或真实Provider试验；这些属于0.5.5b2和0.5.5c。

## 2. 架构与信任边界

```text
内置HistoricalCodingEval目录
        │ 固定revision / tree OID / ls-tree SHA-256
        ▼
Historical Materializer ──git archive──► 私有run/workspace
        │                                      │
        │  fresh one-commit Git baseline       ├─ Agent可见工作区（b2）
        ▼                                      │
materialization.json（最后发布ready）           └─ 未包含后续修复历史/新增测试
        │
        ├─ baseline hidden checks
        └─ final behavior + regression checks
                 │
                 ▼
       EvalTestObservation（退出码/耗时/输出摘要）
```

模块职责如下：

- `evals/catalog.py`只保存经过源码求证的内置任务与检查映射，不接受模型提供的revision、路径或命令；
- `evals/materializer.py`验证来源身份、导出固定Git对象、构造单提交私有仓库并发布可恢复清单；
- `evals/_historical_check.py`是随wheel发布的宿主检查程序，借用基线已有的wire夹具构造输入，不把后续新增测试复制进Agent工作区；
- `evals/checks.py`通过既有`HostProcessRuntime`运行固定检查，只返回`EvalTestObservation`；
- 0.5.5a的评分器保持纯函数，不反向承担工作区或进程生命周期。

Catalog、物化器和检查程序均属于受信宿主代码。被评Agent在b2中只能看到任务Prompt和私有工作区，不得选择检查模式、解释器、来源revision或运行根目录。当前API不是接收任意第三方Eval定义的插件系统。

## 3. 历史任务身份

任务`harnessix-openai-empty-incremental-call-id`冻结以下事实：

| 字段 | 固定值 |
|---|---|
| source revision | `9f24961840fa704e7c7a344c648164d8afe793b7` |
| source tree OID | `c3df320a023537c1e0a6931a758ac67940279658` |
| `git ls-tree -r -z --full-tree` SHA-256 | `d91bdff8b78e85222f16e00b44b6888c563c2986f27ca831b414930ec2623ba4` |
| tracked files | 240 |
| 行为检查 | `empty-id-behavior` |
| 回归检查 | `identity-guards` |
| 测试Profile | `focused` |
| 允许修改 | `src/harnessix/models/_chat_stream.py` |

来源revision、Git tree OID和规范化`ls-tree`摘要同时校验：OID证明Git对象身份，SHA-256避免把评分契约只绑定到Git对象算法，文件数用于物化后交叉核对。完整`CodingEvalTask`继续生成任务指纹，Prompt、预算或检查集合变化必须提升任务版本或产生不同指纹。

行为检查覆盖参数增量为`None`和字符串结束片段两种空ID情况；回归检查覆盖初始身份缺失、真实ID漂移、工具名漂移和类型非法。基线必须表现为行为检查失败、回归检查通过；最小修复后两组均通过。

## 4. 确定性物化流程

宿主必须提供精确仓库根、已存在的私有运行根、受控Git绝对路径和UUID运行ID。物化步骤固定为：

1. 解析Git可执行文件为可执行普通文件，并关闭系统/全局配置、分页器、交互和可选锁；
2. 要求`source_root`等于`git rev-parse --show-toplevel`，禁止借用父仓库子目录；
3. 将Catalog中的40位revision解析为提交，核对提交值、tree OID、`ls-tree` SHA-256和文件数；
4. 在新建0700运行目录内用`git archive`导出固定对象，归档限制64 MiB；
5. 只解包普通文件和目录，拒绝绝对路径、`.`/`..`、`.git`、符号链接、硬链接、设备和其他Tar成员；成员上限100000，总展开字节上限64 MiB；
6. 在工作区初始化新的Git仓库，以固定作者、时间、消息和内容创建一个基线提交；
7. 再次核对新提交树SHA-256、文件数和干净状态；
8. 记录来源归档SHA-256、私有基线提交、Git版本和创建时间，以0600原子写入`materialization.json`；清单写入并同步成功后状态才是`ready`。

新仓库只含一个提交，不包含来源remote和后续修复历史。固定提交元数据使相同来源树得到相同私有基线提交；`run_id`和`created_at`仅属于运行实例，不参与代码树身份。

## 5. 持久化、恢复与失败语义

`materialization.json`采用`harnessix.coding-eval-materialization/v1`，持久保存任务版本/指纹、来源revision/tree/归档摘要、私有基线revision/tree摘要、文件数、归档字节数、Git版本和`ready`状态，不保存宿主绝对路径、隐藏测试正文或凭据。

清单是发布点：解包、Git初始化、提交或核对期间失败会删除本次新建的运行目录；清单完成后相同运行ID只执行重开，不重新导出，也不覆盖Agent已有修改。重开要求：

- 运行目录和`workspace`是普通目录，清单是`O_NOFOLLOW`打开的受限普通文件；
- 运行ID、任务版本/指纹和来源身份全部匹配；
- `HEAD`仍是记录的私有基线提交，`HEAD`树摘要和文件数不变；
- 允许工作树保留未提交修改，以支持Agent中断后的后续恢复和评分。

已有但没有有效ready清单的目录视为不完整或损坏，物化器不会删除、接管或覆盖。来源历史缺失、树不匹配、归档非法、Git失败和清单损坏均以稳定`KernelError.code`拒绝，不把Git stderr、宿主路径或归档内容写入公开错误。

## 6. 隐藏检查执行语义

检查由固定`python -I -B <host-checker> <workspace> <mode>`启动。解释器路径由宿主配置；由于POSIX venv入口通常是符号链接，而`HostProcessRuntime`会把绑定解析到基础解释器，本片在运行目录的`host/`下原子生成一个0700入口脚本。脚本保存被配置解释器的词法绝对路径，从而保留venv的包发现语义；该脚本位于Agent工作区之外，创建内容不接受模型输入。

每个检查使用独立进程、60秒上限、16 KiB双流捕获和128 KiB停止阈值。退出语义固定为：

- `0`：检查通过，形成`passed=true`事实；
- `1`：被测行为不满足，形成`passed=false`事实，不属于运行器故障；
- 其他退出码、超时、取消以外的停止原因或不完整管道证据：`eval_check_infrastructure_failed`，不得进入正常评分；
- `CancelToken`取消：传播`TurnCancelled`，不伪造检查失败。

检查程序只输出固定的passed/failed/infrastructure-error诊断。Eval观察保存检查ID、阶段、退出码、耗时，以及stdout/stderr观察摘要组合后的SHA-256，不保存原始输出。基线阶段只运行`baseline_checks`；最终阶段运行行为和回归检查的有序并集。

## 7. 安全、部署与可观测性

本片缩小了答案泄漏和来源漂移面，但没有提供OS Sandbox。历史代码和其测试仍以宿主用户权限执行，具备该用户可访问的文件和网络权限。因此当前仅允许运行经过评审并固化在Catalog中的Harnessix自身历史任务；不得把动态下载或第三方仓库接入本接口后宣称安全。

运行根应位于工作仓库之外并由服务账户私有持有。源仓库必须具有完整历史；CI的Python和macOS Eval作业使用`fetch-depth: 0`，默认测试不访问网络或调用模型。生产编排器应把物化错误码、任务指纹、运行ID、基线/最终检查阶段、耗时和输出摘要写入受控运行记录，但不得把隐藏检查正文加入Session、模型wire或公开报告。

解释器环境必须预装任务基线所需依赖。本任务使用项目开发环境中的OpenAI SDK和pytest夹具；基础wheel可以导入Catalog和物化器，但单独安装基础依赖不足以运行该历史检查。

## 8. 取舍与后续

### 使用历史真实缺陷，而不是人工植入错误

真实提交同时提供故障实现、修复依据和回归意图，能够验证任务不是为当前Agent路径量身编写。Catalog仍显式复制行为断言，而不直接检出后续测试文件，避免将答案和后续历史暴露到工作区。

### 新建单提交仓库，而不是保留浅克隆

浅克隆仍可能携带remote、配置和可扩展历史边界。`git archive`加单提交基线只保留本次任务所需内容，并为Git状态、差异和HEAD不变评分提供正常仓库语义。

### 允许脏工作树重开，而不是要求每次重新物化

Agent恢复必须保留已归因的未提交修改。重开核对不可变HEAD和基线树，工作树的实际变化由后续Git证据和评分器判断；物化层不抢先删除或回滚。

### 不在b1复制Agent闭环

b1只建立数据集与检查基础。0.5.5b2必须复用现有`AgentRuntime`、`RunTestsAgentBridge`、受管Patch专用端口、唯一Action审批和外部Worker，把同一物化工作区驱动到最终评分；不得为Eval新增绕过审批的Patch或Process执行路径。0.5.5c随后才进行显式授权的真实Provider多次试验。

## 9. 验收

自动测试使用仓库真实历史验证Catalog固定身份、240文件单提交物化、后续修复测试不存在、0700/0600权限、同运行ID脏工作树重开、不完整目录不覆盖、清单符号链接拒绝、来源树不匹配清理、基线行为失败、身份回归通过、最小修复后全部通过、Git只出现允许路径、取消传播、解释器绑定拒绝和异常退出码基础设施分类。

本片新增一份`coding-eval-materialization-v1`公共Schema，不修改Agent v9、Session migration11、Action/Process/Artifact/Patch协议或数据库，不调用模型、API、SSH或远程中间件。0.5.5b2完成前，不宣称真实Agent已完成该历史缺陷。
