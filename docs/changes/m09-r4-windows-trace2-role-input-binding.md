---
doc_type: change-design
status: current
version: 1
code_revision: 5265fdf2d1755b15491f41b770b2d718bab98d2c
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_git_trace2_projection.py
  - tests/governance/test_git_minimum_commit_probe.py
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_failure_projection.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
  - tests/governance/test_windows_git_trace2_input_binding.py
supersedes: []
---

# Windows Git Trace2角色接线与精确发行输入设计

## 1. 文档摘要和实现边界

本设计修正内部Windows材料诊断将wrapper的Trace2 `start.argv[0]`错误地与启动器完整路径比较的问题。
角色必须来自既有固定PE/PDB完整验真，再与实际命令和请求的物理身份关联；
不得通过basename或原始事件自行推断角色。完整实际Worker命令不变，只调整诊断解释器的预期表示。

发布输入基线为`5265fdf2d1755b15491f41b770b2d718bab98d2c`，不是本变更后继原生运行的revision。
新的两件诊断源码和基线已经改变的两件生产源码纳入原16件精确字节合同；
执行时仍必须使用包含全部变更的完整固定提交，不能用基线、旧摘要或仅语义相等的源码运行。

当前限定离线验证为六件治理测试文件共767项通过，507件实际输入前后零漂移。
507件中包括479件受版本管理的生产成员，其中438件为Python源码；不是507件Python源码或全仓测试。
测试不执行Windows、调试器、供应商请求或真实SDK。
原Run `37089114490` 的Git128/Worker2、input proof缺失、SDK未通过和Root未知保持原结果。
角色接线修复不证明Git128的业务根因，不关闭R1、R3、R4、独立Beta或商用门禁。

## 2. 需求背景和问题归因

固定现场失败的有限Trace2记录显示`STREAM_BINDING_MISMATCH`。
源码与固定发行资料证明：经验真的wrapper可将Trace2首项表示为字面量`git.exe`，
而Worker实际启动参数首项仍为原启动器完整路径；core的DIRECT表示保持原首项。
该差异足以解释一种诊断误拒绝，但SID不一致等情况也可产生相同有限记录，不能认定唯一现场根因。

原预检在外层Python进程验证PE/PDB，随后CDB启动独立Python进程执行两个固定案例。
两者不共享内存，不能声称已有角色结果自动传入探针。
解决方案是在探针原fixture安装hook前，复用既有验真算法只读复核原预检准备的符号文件，
将完整验真的角色关联保存在当前探针内存，结算后清空。

## 3. 设计目标、非目标与取舍

- 只有实际PE/PDB和物理身份都匹配时，wrapper采用`("git.exe", *argv[1:])`，core采用原argv。
- 后21个参数、原ProcessSpec、Plan、批准、Owner、Job及Worker完全不改。
- 全部角色来源成功后一次性发布，不暴露部分绑定；失败不能猜测或回退成DIRECT。
- 原完整raw/MAC/EOF/protection前置、SID、profile、重复start及有限字节/事件预算保持。
- 不增加公开诊断字段、执行权限、自动下载、后台服务或数据库。
- 已知源码变化必须重新固定身份，原冻结拒绝和历史原生失败完整保留。

不选择basename宽松匹配，因为它无法证明当前可执行文件是固定官方wrapper。
不解析未验真事件反推角色，因为输入流不能为自身的来源授信。
不将角色写到环境变量或文件，因为诊断无需新增跨进程身份传输面。
额外PE/PDB只读复核仍消耗原总期限，未实测其Windows现场耗时，不增加期限。

## 4. 总体架构、模块边界与源码映射

![总体架构](../validation/windows-trace2-input-binding-2026-10-03-v1/architecture.png)

| 模块 | 责任 | 明确不承担的责任 |
|---|---|---|
| [`contract.py`](../../scripts/windows_git_native_branch_observation/contract.py) | 验证固定metadata及16件源码精确字节 | 不自动刷新，不授予执行权限 |
| [`preflight.py`](../../scripts/windows_git_native_branch_observation/preflight.py) | 定位原选中Git及固定布局，准备符号 | 不以目录名授角色 |
| [`identity.py`](../../scripts/windows_git_native_branch_observation/identity.py) | 原PE/PDB大小、SHA、GUID/age、RVA和分支字节验真 | 不改变诊断或业务成绩 |
| [`git_minimum_commit_probe.py`](../../tests/product_config/git_minimum_commit_probe.py) | 当前fixture只读复核、角色生命周期、原13 hook和结算 | 不新开Owner，不写业务状态 |
| [`git_trace2_projection.py`](../../tests/product_config/git_trace2_projection.py) | 绑定原命令/请求与角色，调用原封闭流解释器 | 不执行Git，不输出动态身份 |

## 5. 核心流程、时序与数据流

### 5.1 角色来源流程

![验真与投影流程](../validation/windows-trace2-input-binding-2026-10-03-v1/role-flow.png)

模式为`off`或非Windows时不触发角色复核。
Windows显式`stderr-event-v1`在原fixture安装hook前调用`_bind_trace2_roles`：
先清空缓存；要求原绝对basetemp；从其父目录只读定位原`symbols/{wrapper,core}/git.pdb`；
使用原`selected_paths`定位程序，符号真实路径不得逃出原私有输出目录。
每一对验真前后物理身份必须一致，两对角色、路径及选中程序均符合固定集合后才发布tuple。
任何来源异常被原`_safe`隔离，探针标记不完整，不吞掉实际业务异常或修改其状态。

### 5.2 生命周期时序

![调用时序](../validation/windows-trace2-input-binding-2026-10-03-v1/sequence.png)

正常路径在原完整Completion之后消费其`input_proof`和已验真stderr。
失败观察仅在原receipt/raw/protection守卫之后消费原字节，未满足前置不得投影。
投影采用已认证角色，但仍执行原profile、SID、start唯一性、完整EOF与返回码一致性判断。
关闭原hook和完成结算后，在`finally`清空角色以及prepared/handle/protection引用；
诊断绑定不能跨Case、Turn或重开复用。

### 5.3 数据流程与持久化

![数据流](../validation/windows-trace2-input-binding-2026-10-03-v1/data-flow.png)

程序真实路径和物理摘要只在当前内存关联，`repr=False`隐藏动态字段。
公开输出继续只有原七字段固定枚举；不输出路径、SID、PID、argv、stderr正文、PDB或凭据。
既有`Probe.render`、CaseSink和正式gate不改。
本设计没有Session迁移、MAC补签、业务数据库写入或新恢复账本。
固定合同是版本管理的发行输入，不是在线自动再绑定机制。

## 6. 类设计、接口设计、数据结构和重点字段

| 类型/接口 | 入参与出参 | 核心不变量 |
|---|---|---|
| `_Trace2RoleBinding` | frozen、slots数据类：`executable`、`identity`、`role` | 前两字段不进入repr；角色仅wrapper/core；不是权限 |
| `Probe.trace2_roles` | 当前探针只读tuple，初始为空 | 全部验证成功后发布；失败和结算清空 |
| `_bind_trace2_roles(probe, basetemp)` | 原探针、原fixture私有目录；无公开结果 | 完整官方配对，符号不逃逸，程序身份前后相同 |
| `_operation_start_argv(operation)` | 原Operation；返回预期tuple或None | 22项参数，唯一完整路径匹配，真实request/command身份及argv全等 |
| `project_operation_trace2(operation, stderr, git_returncode)` | 原已认证内存结果 | 缺角色保持`STREAM_BINDING_MISMATCH/UNKNOWN`，不能fallback成功 |
| `source_checks(repository, contract)` | 原16件真实文件；完整结果列表 | 长度和SHA同时匹配LF或唯一完整LF→CRLF转换 |

`identity`来自原`_executable_identity`，并非basename、随意SHA或Trace2事件字段。
`command.executable_identity`、`request.git_executable_identity`与角色身份必须相同；
`command.argv`必须等于原`request.git_argv`。
角色只影响Trace2预期首项，不改变请求、批准指纹、Worker执行或退出证明。

## 7. 核心业务伪代码

```text
原fixture开始：
    清空旧角色
    若Windows且显式Trace2：
        读取固定合同与原selected_paths
        对原wrapper/core两对：
            定位原符号并检查真实路径边界
            记录原物理身份，复用原完整check_pair，再复核物理身份
            任一失败 -> 原探针incomplete，不发布部分结果
        验证恰好两角色、不同路径、选中程序属于原集合
        一次性发布不可变角色tuple
    安装原13 hook，执行原两个固定案例

原完整Completion或原失败raw守卫之后：
    验证原22项argv、角色类型、唯一完整程序路径和三方物理身份
    验证原command.argv与request.argv全等
    wrapper -> 仅预期首项使用git.exe；core -> 原argv
    未知 -> 原STREAM_BINDING_MISMATCH/UNKNOWN
    已知 -> 原_Stream校验profile、SID、唯一start、EOF、预算及退出一致性

原结算finally：
    清空角色和Operation内部引用；不持久化，不重放
```

## 8. 精确发行输入与历史冻结门禁

原16件输入成员和顺序不变，只更新下列四行的长度、SHA及完整CRLF长度/SHA：

1. [`git_delivery_process.py`](../../src/harnessix/product_config/git_delivery_process.py)：基线已发布共享宿主实现。
2. [`git_material_process.py`](../../src/harnessix/product_config/git_material_process.py)：基线已发布实现摘要绑定。
3. [`git_minimum_commit_probe.py`](../../tests/product_config/git_minimum_commit_probe.py)：新角色来源和生命周期。
4. [`git_trace2_projection.py`](../../tests/product_config/git_trace2_projection.py)：新角色与原命令绑定。

`base_revision`固定为5265发布输入基线；将上述17个叶字段恢复后，metadata与基线完整深比较相同。
`contract.py`仅替换`CONTRACT_SHA256`字面量，其他字节完全相同。
资产、官方发行、两对PE/PDB、selector、历史结果和20/45/300/240秒预算不改。
最终metadata SHA为`902dbdf1f7e837fff3102c1006984b0e4dd177e364e88a2162f21070a179026c`。

原九件整体字节门禁没有删除：五件未变模块继续与原历史Git对象全文比较；
四件合法变化模块改用新的固定整体长度和SHA，任何后继未知变化继续失败。
新增18件原源码片段逐字保护覆盖原hook、raw前置、流解析、异常透明性和`Probe.render`。
旧合同在当前源码上仍真实拒绝，旧失败、旧冻结包和旧提交不改写。
metadata差分采用类型敏感的JSON Pointer集合，恰为17个允许叶值；
整数、浮点数和布尔值不能因Python相等而被视为同一合同类型。
校验通过只证明声明的源码输入合法，不证明整个仓库或实际Windows业务执行成功。

## 9. 失败、取消、恢复和安全边界

- 未知角色、类型错误、身份漂移、重复绑定或参数不符：固定未知，不猜测。
- 符号缺失、逃逸、配对失败或选中程序改变：缓存清空，不发布半份角色。
- MAC、raw、EOF、protection或原Completion缺失：不得通过角色信息升级完整性。
- 原业务取消、超时和Owner结算路径不变，诊断没有新增进程或取消任务。
- 原240秒watchdog、300秒工作流期限不变，不因符号复核延长。
- 模式off保持默认行为；原生修复仍需固定新提交的独立运行。
- 本地/CI观察超时不是终态；只能查询原句柄，不能重启未知运行。
- 已知源码变化必须先重新评审固定身份；不得删除检查、跳过失败或自动接受新摘要。

## 10. 部署、验证与可观测性

该模块仅为内部Windows固定原生观察探针，不新增用户产品配置、服务、中间件或网络访问。
正式运行复用[`windows-git-native-branch-observation.yml`](../../.github/workflows/windows-git-native-branch-observation.yml)，
固定实际提交、`execute=true`、`expected_revision`一致及`GITHUB_RUN_ATTEMPT=1`。
原预检准备符号，探针只读复核；不能将未准备符号的普通目录当作正式角色来源。
公开artifact仍只上传`result.json`与`result-sha256.json`，原私有日志不上传。

| 验证阶段 | 结果与覆盖 |
|---|---|
| 角色候选原完整集合 | 610通过、1旧冻结身份失败；保留，不冒充全通过 |
| 身份接合前RED | 4项真实失败：当前来源、metadata边界、parser常量及原整体冻结 |
| 新测试初次整合 | 742通过、17测试夹具失败；修正实际API返回字段及目录已存在处理，未改产品断言 |
| 格式化后原完整集合 | 759通过，原阶段保留 |
| 独立审查发现的类型差分缺口 | 8项新增负例先全部失败，只修测试逻辑，不改metadata或产品 |
| 类型敏感修复后最终完整集合 | 767通过，0失败/错误/跳过，无排除旧失败测试 |
| 精确输入 | 507件前后零漂移，原16件逐项接受，新未知单字节逐项拒绝 |
| 静态门禁 | 7件变更Python Ruff check和format通过；没有生产Python行为变化 |

有限独立审查及最终身份详见[验证资料](../validation/windows-trace2-input-binding-2026-10-03-v1/README.md)。
不累计旧611/767结果，不以离线synthetic PE/PDB或memory模型冒充官方现场验真。
Windows现场耗时、实际Git效果、完整SDK成功和消费者平台验收继续开放。
