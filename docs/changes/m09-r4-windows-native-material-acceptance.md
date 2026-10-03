---
doc_type: change-design
status: current
version: 2
code_revision: 0f1948c3a258943698a8fe3e4309b81e78b8d5b3
owners: [core]
modules: [delivery, product_config, processes, workspace, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/governance/test_windows_native_material_acceptance.py
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_cas_integration.py
  - tests/processes/test_windows_raw_receipt.py
  - tests/workspace/test_snapshot_request_refactor.py
  - tests/workspace/test_snapshot_capacity.py
  - tests/delivery/test_git_projection_capacity.py
  - tests/governance/test_readability_policy.py
supersedes: []
---

# Windows完整Git材料的并行原生验收：总体与详细设计

## 1. 需求背景与现有证据

固定0583b53的最低SHA256 Commit专项Run37134072312已经通过；它不能替代完整材料容量与认证回执矩阵。
同候选常规CI Run37134036729的Windows Job111234779832仍失败：前置NTFS写链、Git读取与取消步骤成功，
认证raw／Git基准聚合步骤failure。该步骤运行308秒，配置五分钟；时间接近上限不能证明每个case失败或唯一超时根因。
未读取原始CI业务日志、stderr或CDB日志，现有事实只能定位到合并的27个选择器，而非具体断言。

原合并组同时包含认证输出、完整对象输入、CAS及树引用验真。重新执行相同失败合并组不会提供分组结果；
新的CI候选将原选择器按现有模块拆为三个独立Windows矩阵成员，沿用普通pytest及原步骤结论。

## 2. 设计目标、非目标及取舍

- 27个原选择器完整保留、恰好一次；不删除失败、缩减8MiB容量、对象种类或SHA1／SHA256覆盖。
- 在互不共享工作区的原生Windows runner上并行执行，某成员失败不得取消其他成员。
- 任何成员失败仍使其检查失败；没有continue-on-error或替换Grader。
- 原NTFS、读取、重启、备份、恢复及后继广泛回归步骤保持，只迁出原聚合步骤。
- 不新增采集器、日志格式、诊断插件、PE／PDB下载或模型请求；最低Commit专项及18输入完全不改。

分组调度的非目标：不修复生产源码，不装配默认Git产品、Backup v2，不承担消费者Windows11或R3质量验收。
拆分是CI调度与验收边界变更，不将它称为业务故障已修复。新原生结果暴露的Snapshot热点回归
由第10节单独设计并整改，不能将调度变化与后续生产源码修复混为同一项。

每成员保留原测试步骤五分钟上限；由一个合并五分钟改为三个可并行五分钟成员。
因此总runner分钟与最坏资源占用可能增加，不能声称总CI预算未变。实际pytest选择器总量保持，
额外开销来自三份隔离checkout／依赖准备；不提高原20秒命令、45秒材料操作或最低专项240／300秒期限。
选择并行而非串行三组，避免正常执行等待时间按三个上限累加；实际排队和耗时以新Run为准。

## 3. 总体架构、模块边界与源码映射

```mermaid
flowchart TB
  C[同一冻结提交与锁定依赖] --> R[认证raw及Git基准 16选择器]
  C --> I[完整对象输入与安全 6选择器]
  C --> S[CAS及完整引用 5选择器]
  R --> A[独立原生pytest 原断言]
  I --> B[独立原生pytest 原断言]
  S --> D[独立原生pytest 原断言]
  A --> E[原GitHub检查结论 任一失败不放行]
  B --> E
  D --> E
```

矩阵只改变[CI调度](../../.github/workflows/ci.yml)。每个runner的fixture、Workspace、SQLite、Owner及临时Git
仓库独立生成，不在成员之间传递PID、Proof、批准、CAS或会话。生产代码仍走原宿主、Supervisor及Worker：

| 模块与源码 | 已有职责 | 本次变化 |
| --- | --- | --- |
| [git_material_process](../../src/harnessix/product_config/git_material_process.py) | 完整输入准备、批准、staging和原Process宿主 | 无 |
| [git_material_worker](../../src/harnessix/delivery/git_material_worker.py) | 原命令、只读快照、namespace后验及完整回读 | 无 |
| [Windows原生端口](../../src/harnessix/delivery/git_material_native_windows.py) | 原真实句柄、DACL、类型、身份及共享保护 | 无 |
| [材料输入测试](../../tests/product_config/test_git_material_input.py) | 各对象、格式、容量、独立批准回读及失败保护 | 全文件保留 |
| [CAS集成测试](../../tests/product_config/test_git_material_cas_integration.py) | 原CAS完整字节到原Owner及独立回读 | 全文件保留 |
| [调度治理测试](../../tests/governance/test_windows_native_material_acceptance.py) | 对冻结原选择器多重集合及其他步骤作精确比较 | 新增测试域守卫 |

没有新增生产类、端口或数据结构。治理函数仅解析已有YAML、命令token与Counter，不承担执行授权。

## 4. 接口设计、字段与数据结构

| 字段／接口 | 契约及重点解释 |
| --- | --- |
| job windows-git-native-material | windows-latest；与原核心Windows job独立，不使用其临时状态 |
| matrix.group | authenticated-raw、object-input、cas-reference三个静态值，作为简短检查名称 |
| matrix.selectors | 版本库内固定pytest选择器字符串；不是外部工作流输入或用户命令 |
| strategy.fail-fast | false；一个成员失败不取消另外两个成员 |
| timeout-minutes | 原pytest步骤每成员5；不是业务单次操作预算 |
| pytest参数 | 原uv run pytest及-vv、-o faulthandler_timeout=60，保留原断言和错误退出 |
| 原选择器多重集合 | 固定8162953原聚合27项；Counter同时检测遗漏、重复及替换 |
| 其他步骤完整列表 | 删除原聚合一步后应逐对象等于旧列表；禁止顺带改安装、备份、恢复或安全门 |

完整六件输入组为git_object_material、git_material_input、snapshot_lifecycle、native、directory_access及owner_exit。
CAS五件为git_material_cas、cas_write_authority、git_object_references、git_tree_closure及cas_integration。
其余原16项属于认证raw及Git基准，包括原容量基准的两个精确测试；选择器按旧顺序分配。

## 5. 核心流程、时序、数据流与伪代码

```mermaid
sequenceDiagram
  participant CI as 原GitHub CI
  participant R as 独立认证raw runner
  participant I as 独立输入runner
  participant S as 独立CAS runner
  CI->>R: 同一提交 锁定依赖 16选择器
  CI->>I: 同一提交 锁定依赖 6选择器
  CI->>S: 同一提交 锁定依赖 5选择器
  par 原pytest并行
    R-->>CI: 原步骤结论
  and
    I-->>CI: 原步骤结论
  and
    S-->>CI: 原步骤结论
  end
  Note over CI,S: 任一失败保持FAIL 不取消其余成员 不拆造单case结果
```

```mermaid
flowchart LR
  Old[固定旧27项选择器] --> Counter[原多重集合]
  New[矩阵三个静态选择器集合] --> Union[串接并保留每项次数]
  Union --> Match[与原Counter精确相等]
  Counter --> Match
  Match --> Test[原生pytest 原容量及批准断言]
  Test --> Metadata[现有Job及step结论元数据]
  Metadata --> Scope[只对实际分组登记结果 未执行不视为通过]
```

```text
read original CI step at fixed 8162953
parse original pytest selectors and trailing arguments
partition by the existing input and CAS file sets; all others remain raw/baseline
require Counter(all matrix selectors) == Counter(original selectors)
require every other Windows core step unchanged
checkout the same candidate independently for each matrix member
run the unchanged pytest command arguments with the original per-step timeout
retain every failure; read only existing Run/Job/step conclusion metadata
do not promote group success to full product or commercial acceptance
```

## 6. 失败、取消、恢复与持久化

失败、异常、超时均保留原pytest非零状态；矩阵fail-fast=false只保障其他成员可继续，不将FAIL转换为PASS。
工作流取消仍取消各runner；每个测试中的原CancelToken、进程回收、Lease及UNKNOWN语义不改。
不是业务恢复实现，不能以重启runner替代持久化或重放未知副作用。

测试使用各自临时数据库和仓库，原业务持久化仍由现有Store负责；不创建新的共享CI数据库。
证据仅保存现有元数据、设计前置、精确源码摘要、本机回归与图示，不发布临时目录、正文、批准或凭据。
旧FAIL不可覆盖；观察超时只继续核对同一活跃Job，不重新dispatch或重跑旧失败候选。

## 7. 安全与可观测性

新job沿用固定checkout／setup-uv及uv sync --locked --all-extras --dev，没有新增secret、网络执行入口或权限。
选择器全部来自受评审源码，禁止外部matrix输入。原contents: read全局权限保持。
仅使用原pytest与GitHub检查结论，不读取业务日志，不新增stderr解释或采集格式。
最低专项通过不能提升历史Root UNKNOWN；各成员PASS也不证明消费者Windows11或完整产品交付。

## 8. 测试、发布与回退

先以旧YAML运行新守卫，确认缺少矩阵时RED；按上述唯一迁移实现后GREEN。
守卫覆盖选择器Counter、静态分组、fail-fast、runner、原参数／期限、无失败忽略以及其他原生步骤不变。
另以collect-only列出同一27选择器的当前参数化节点并冻结清单；本机执行三组关联回归只证明POSIX范围，
Windows-only跳过单列，不能计为原生通过。原最低专项18输入必须仍一致。

新源码与文档静态、精确Secret及三个图示渲染通过后正常推送一次新候选，使用该候选自动CI取得各成员结论。
不额外手动重跑最低专项或旧失败合并组。若某成员仍失败，保留实际范围并针对该成员继续闭环，不放宽门槛。
回退只还原CI分组和测试域守卫，不修改公共Schema或持久状态；旧失败证据仍保留。

## 9. 当前范围与商用退出条件

前三个分组的设计是原完整验收范围的并行调度，不增加产品特性，不删除历史代码或更改业务安全契约。
固定0f1948c的三个分组现已取得原生通过；这不代表后续第10节源码修复也已通过原生验收。
完整Git／Backup v2、真实R3质量与费用未决、消费者三平台、
独立Beta及同候选R1～R6继续按原完整目标验收，不以CI分组替代商用结果。

当前实现已按上述分组迁移，治理7项通过。collect-only的固定清单实际1306节点，
认证raw386、对象输入303、CAS引用617；三组节点多重集合与原合并组完整相等。
本机和新原生候选结果另行记录，不以收集成功视为业务或原生通过。

固定[Run37136790041](https://github.com/carrie1988/Harnessix/actions/runs/37136790041)、attempt1、
head0f1948c的认证raw Job111242837376、对象输入Job111242837311及CAS引用Job111242837410均success，
pytest步骤分别141／180／119秒。1306是本机完整收集节点数；原生步骤元数据不提供逐项计数或跳过信息，
不得将它写成原生1306项无跳过通过。核心Windows Job111242837353五个业务步骤success，
随后原可读性检查failure，后继广泛回归skipped；整个Run仍failure。文档及容器沙箱Job success。

新治理显式以UTF-8读取当前YAML与冻结Git源码，防止Windows默认locale影响中文步骤名称；
对应读取守卫通过。三组本机关联实际1283通过／23 Windows相关跳过、零失败／错误；治理7项单列。
首次文档检查发现三个语义章节标题未匹配规范，内容已具备但规范命名不完整；修正标题后重新检查，原发现保留。

## 10. 生成报告漂移与Snapshot资源准入职责收敛

### 10.1 实际结果与已复现原因

固定0f1948c的Run37136790041三个完整Windows矩阵成员全部success；核心Windows写入、读取、重启、
完整状态备份与恢复五步success。该job及Linux／macOS后续卡在原可读性检查，不以三组通过宣布完整CI通过。
本机原检查实际复现：capture_workspace_snapshot长度136超过原133，复杂度30超过原29，且最终报告漂移。
原600／100／20基础阈值及热点例外不得扩大。报告漂移来自已发生的源码变化，不能简单更新报告而忽略两个真实热点回归。

### 10.2 最小结构设计、接口与源码映射

[快照捕获](../../src/harnessix/workspace/snapshot.py)中的请求复制、cwd/read补入、256项准入形成单一职责，
提取为包内_snapshot_resource_requests(resources, cwd, platform)，返回原同顺序的list请求。
原主函数仍先开根、复核外部根与观察cwd，再在同一位置调用helper；没有提前或延后安全检查。
原逐叶观察、重复拒绝、总量保护、排序、Root及revision payload全部保留，公开接口与Schema不变。

| 重点字段／接口 | 原行为与约束 |
| --- | --- |
| resources | 调用者完整Sequence，不删叶或裁剪；主函数异常栈仍保留原入参 |
| cwd_key／keys | 原workspace、平台路径比较键、read模式；不将别名当新资源 |
| requested | 原list副本，缺少显式cwd/read时只补一次；补入后仍执行256项拒绝 |
| WorkspaceSnapshot | 原所有字段、规范化、排序和selected-resources-sha256/v1摘要保持 |
| generated final report | 原scripts/readability_report.py从当前src/harnessix AST生成，不手工伪造指标 |
| policy | governance/readability-policy-v1.json原字节不变；热点133／29及基础600／100／20不放宽 |

```mermaid
flowchart TB
  Capture[原capture主流程 根及cwd观察] --> Requests[包内请求副本 补cwd及原256准入]
  Requests --> Original[原逐叶观察 排序及同一payload]
  Original --> Snapshot[原Snapshot字段与摘要]
  AST[当前生产源码AST] --> Policy[原阈值及热点上限 不变]
  Policy --> Generate[原生成器更新当前最终报告]
  Generate --> Gate[原完整可读性检查 不绕过]
```

```text
执行原平台规范化、根打开与cwd观察
requested = 按原顺序复制全部resources
仅在原比较键不存在时补入原cwd/read请求
requested数量超过256 -> 原KernelError，不观察叶子
执行原逐叶观察、重复检查、字节限额、排序与payload摘要
在当前源码满足原policy之后，使用原生成器更新最终报告
```

### 10.3 验证、失败与兼容

先保留原可读性RED和指标差分；结构提取后运行原Workspace容量、Git投影容量及新前后源码差分回归。
新差分从固定0f1948c加载原模块，只用于隔离测试；对相同实际临时根比较合法Snapshot全模型及超限错误，
包括显式／隐式cwd、256边界与重复请求。原入参在capture异常帧保留，原根关闭与KernelError不变。
不更改限额、路径／DACL、跨根访问、资源顺序或既有测试断言；没有状态迁移，旧批准不得跨实现复用。

原生成器只有在不改变policy且检查满足后更新最终报告。继而执行同一--check／--check-final-report，
所有初始发现保留，指标增长与热点减少分别列明；不通过删checker、改报告比较或抬上限恢复绿色。
原18材料输入及SDK／Owner／Proof门保持；完整Git／Backup v2、R3、消费者及Beta仍需原目标验收。

当前结构提取后capture为124行、复杂度27，低于原热点133／29；全局100行阈值及原热点例外仍存在，
不宣称所有函数已低于100行。原policy摘要保持
`176ee35bafa474d71f29fe20c484853d6a326ceb2dcf0188193f34b031b75f2c`。
首次八个新差分案例与原Snapshot、Workspace容量、Git投影容量、可读性及分组治理六个文件合计
50通过／4个Windows相关跳过、零失败／错误。首次关联运行因Ruff格式化后生成报告失配而失败，
该失败保留；按最终源码重新生成后，原检查及关联回归通过。修复提交后的新原生和广泛CI仍须独立验收。

同一未提交源码上的三个完整材料分组再次并行实际1283通过／23个Windows相关跳过、零失败／错误，
分别367／19、299／4、617／0；上述原1306节点与选择器没有减少。全部18专项输入逐件摘要仍匹配，
原policy字节一致。安装及不同版本升级治理两个文件41项通过，只证明既有验收器的正反例，
不将它计为三平台实际安装、升级、真实编码或商业发布通过。

独立窄审查未发现P0／P1；P2指出最终Snapshot排序会掩盖请求观察顺序，模型相等不能单独证明
请求副本保序。补充四个直接序列断言，覆盖POSIX／Windows比较语义及显式／隐式cwd，使用混合location
和非字典序请求，比较完整列表及原输入不变；显式cwd保留原位置，隐式cwd仅尾补一次。
这些纯请求断言不替换原生观察器，也不将Windows逻辑参数测试计作Windows原生验收。
补充后请求提取测试共12项，最终六个关联文件58节点、54通过／4个平台跳过、零失败／错误；
实际生产源码438个文件的类型检查通过。新增顺序断言由主线程闭环，原独立报告保持原审查源码与测试摘要，
不宣称它覆盖后续新增的四个断言。
