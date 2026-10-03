---
doc_type: change-design
status: current
version: 2
code_revision: cf35055668f9529c9a677e5226a98bbd40c4cefd
owners: [core]
modules: [delivery, product_config, processes]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_snapshot_lifecycle.py
  - tests/product_config/test_git_material_stream_ownership.py
  - tests/product_config/test_git_material_native.py
  - tests/product_config/test_git_material_owner_exit.py
  - tests/product_config/test_git_object_material.py
  - tests/product_config/test_git_delivery_process.py
supersedes: []
---

# 固定 Git 对象完整输入总体与详细设计

## 1. 需求背景与问题定义

[完整业务闭包设计](m09-r4-git-delivery-business-backup-closure.md)需要持续多 Patch、完整对象材料、
独立批准、持久交付和备份恢复。[完整对象读取](m09-r4-git-object-material-read.md)已保留 blob/tree/commit、
SHA1/SHA256 和完整 8MiB 正文，但不能解决向 Git 提交同样大小的输入。
原 Process 控制输入和普通 Git 流额度均为 1MiB，不能提高通用输入上限，也不能截断材料后执行。

另一关键问题是取消语义：把大正文分块送入 Git 的管道，进程死亡时 Git 可能把已经收到的前缀当作一个
完整 blob 写入对象库。管道 EOF 不等于批准正文的 EOF。仅验证退出后 stdout 或材料路径摘要不能消除该风险。

本方案把原 Supervisor 启动的根进程固定为受信 stdlib 子程序：原 stdin 只承担小握手，完整正文另行核验；
Git 启动前必须持有已经完整写入、关闭全部写端并复读验真的只读普通文件句柄。

## 2. 设计目标、范围与非目标

1. 保留 blob/tree/commit、SHA1/SHA256 和每对象 8MiB，空 blob/tree 与合法最小 commit 均有对应语义；
   不使用 `--literally` 把不合法 tree/commit 当成成功对象。
2. 复用原 ExecutionPlan、批准、Process Owner、Lease、取消、原始流 MAC 和共享绝对期限；
   不新增 Owner 协议、数据库或自动批准路径。
3. 完整输入在落盘和 Git 启动前，通过原 Owner 已校验并冻结的同一保护集合；有效空集合与缺少来源分开。
4. 固定目标 common/objects、Git 程序、环境、安装代码和基础解释器，拒绝配置或身份漂移。
5. 原过程证明只能认证子程序的输出。对象业务后置条件必须另取批准，沿完整 `cat-file --batch` 回读验真。

当前实现是内部受控 IO 准备与执行能力，不新增默认模型 Git 写 Tool，不授权 Ref/Commit/Push，
不完成 CAS 耐久登记、Git 认证业务账本、Backup v2 或跨崩溃阶段登记。
安装包和相同用户的受信宿主属于 TCB；路径/句柄核验不宣称阻止任意相同 UID 外部程序攻击。
本文前置 Revision 是研究基线，未宣称该基线提交已经包含新增实现。

## 3. 源码研究、架构决策、风险与取舍

| 方案 | 结论 | 原因 |
|---|---|---|
| 仅向 Git 传递 CAS 路径 | 不采用 | 0600 不等于不可变；路径可换、同 inode 可改，摘要观察不是封印 |
| 扩充 Owner 协议传大输入/FD | 本能力不采用 | 两平台控制协议、捕获和回执需同步变更，已有材料目的不应提升通用权限 |
| 原小 stdin + 完整 RO 普通文件快照 | 采用 | 原 Owner 保持单一；Git 前已取得完整 EOF、长度、SHA 和 OID，死亡不会把管道前缀补成对象 |

原 [ProcessSpec](../../src/harnessix/processes/supervision_contracts.py) 的 stdin 描述实际根进程输入，
不是另一个不可见的控制协议。原 [Supervisor](../../src/harnessix/processes/supervisor.py) 启动前构造保护封套；
宿主不能在此之前先读一个可轮换来源再假定等同于 Owner 读取结果。

Windows 创建和关闭行为依据
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)：
从创建开始设置删除关闭标志，删除等待所有原始及复制句柄关闭。
权限降级依据
[DuplicateHandle](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle)：
显式请求读权限且不使用 `DUPLICATE_SAME_ACCESS`，不按路径重新打开快照。
这是 API 依据，不代替新候选的 Windows 原生执行证据。

目录及既有对象保护句柄必须请求实际 `FILE_READ_DATA/LIST_DIRECTORY`，不能仅设属性读和分享数字。
[微软共享检查算法](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/8c0e3f4f-0729-49f4-a14d-7f7add593819)
只对相应的数据/执行/写入/删除访问进行共享冲突检查；属性句柄不能作为同等保护证明。
配置值按照引号外注释解释 `#`/`;`，引号内字符保持；无效引号、禁止 include/filter 和兼容格式仍拒绝。

## 4. 总体架构与模块职责

```mermaid
flowchart TB
    H[受信交付宿主] --> P[GitMaterialPreparation]
    P --> A[原 ExecutionPlan 与批准]
    A --> O[原 Supervisor 与 Owner]
    O --> W[固定 stdlib 根子程序]
    O --> S[冻结本次保护集合]
    S --> G[宿主全正文保护检查]
    G --> F[私有完整 staging 文件]
    F --> W
    H -->|不超过64KiB的原stdin握手| W
    W --> R[完整 RO regular 快照]
    R --> C[固定 Git hash-object 子进程]
    C --> D[绑定 common 的对象库]
    W --> Q[小型生产者证明]
    Q --> V[原 MAC/raw/EOF 核对后解码]
    V --> I[新批准的完整对象回读]
    I --> B[待接线的认证业务登记]
```

`GitMaterialPreparation` 只从原宿主取得命令材料、私有目录和关闭状态；它没有执行权限。
唯一执行入口仍是 `GitDeliveryProcess.run`。根 PID 是基础 Python 子程序 PID，不是其内部 Git PID。
Git 子进程不新建 session/group、不使用 shell、不 breakaway，留在原 POSIX 进程组或 Windows Job 中。

| 源码 | 单一职责 |
|---|---|
| [git_material_process](../../src/harnessix/product_config/git_material_process.py) | 准备用途、固定启动参数、保护后 staging、线程结算和自有文件清理 |
| [git_delivery_process](../../src/harnessix/product_config/git_delivery_process.py) | 原批准/Owner 编排、双流验真、生产者 PID 对照和不确定效果优先级 |
| [input_contracts](../../src/harnessix/delivery/git_material_input_contracts.py) | stdlib 冻结数据、严格 canonical JSON、用途和实现摘要、生产者证明 |
| [native](../../src/harnessix/delivery/git_material_native.py) | 路径/配置控制集合、对象命名空间、有界完整读取、完整 RO 快照 |
| [native_windows](../../src/harnessix/delivery/git_material_native_windows.py) | 本地 NTFS 句柄、私有 ACL、拒共享写/删除及从创建开始的快照生命周期 |
| [worker](../../src/harnessix/delivery/git_material_worker.py) | 小握手 EOF、固定 Git argv/env、共享期限、子进程输出上界和证明生成 |

## 5. 接口设计与重点类设计

```text
GitDeliveryProcess.prepare_object_write(
    cwd, repository_binding, common_directory, material,
    source_digest, shared_budget, timeout
) -> PreparedGitProcess

GitDeliveryProcess.run(prepared, original_plan, cancel,
                       shared_budget, checkpoint) -> GitProcessCompletion

prepare_material_stdin(write, frozen_protection, cancel, budget)
    -> StagedGitMaterial

decode_manifest(canonical_bytes) -> GitMaterialInput
decode_proof(canonical_bytes, original_request) -> GitMaterialProof
```

`source_digest` 是受信宿主的来源绑定，不接受模型自报摘要作为产品授权证明。
调用该内部接口本身不证明摘要已经来自原发布 Patch/CAS；正式产品桥接仍需核验原来源。

`PreparedGitProcess.command` 保留内层 Git 命令及完整 body 摘要，`spec.argv` 是固定子程序启动；
两个角色不能混淆。`write` 与原读取用途 `material` 互斥。批准参数同时绑定两者，不持久化 body 正文。
`GitMaterialExecution.verify` 重验请求、完整不可变 bytes、固定启动入口、物理身份及控制文件集合。
`GitProcessCompletion.input_proof` 与回读 `material` 分开，不能将一份过程证明当作对象业务完成。

## 6. 数据结构、字段解释与容量

| 数据/字段 | 含义与核验 |
|---|---|
| `nonce`、`body_ref` | 单次随机用途与私有名称；握手、Plan 和证明必须一致，不按旧过程重放 |
| `source_digest` | 原受信来源摘要；当前 IO 不自行伪造来源业务登记 |
| `body_sha256`、`body_bytes` | 完整正文 SHA256 和长度，正文为严格 bytes，范围 0～8MiB |
| `expected_oid`、`object_format`、`object_type` | 按 Git 类型/长度头与正文计算 SHA1/SHA256；仅三个固定类型 |
| `repo/common/objects_*identity` | 实体目录和原定位目标，不能重定向到别的对象库 |
| `control_files` | config、可选 worktree config 和 worktree 指针闭包的完整 SHA/长度/身份；最多8件，每件最多1MiB |
| `git_argv`、`git_environment` | 原 Runner 固定材料；无过滤、无懒取对象、仅 file 协议、无继承用户配置/凭据 |
| `runtime_identity` | 基础解释器实体版本；不用 Windows venv launcher PID 冒充子程序 PID |
| `implementation_digest` | 四个 stdlib 模块和两个包入口的完整内容摘要；宿主适配实现另由原批准绑定 |
| `purpose_digest` | 除自身之外的全部用途字段 canonical 摘要，含 nonce 和绝对期限 |
| `expiry_monotonic_ns` | 原 `GitOperationBudget` 的绝对单调期限；每阶段不能获得新预算 |
| `manifest_sha256` | 实际小 stdin 的 canonical 字节摘要，固定 argv 再绑定一遍 |
| `source_eof`、`snapshot_*` | 子程序观察完整 stage 和 RO snapshot；长度/SHA须等于原正文 |
| `producer_pid` | 实际子程序 PID，宿主解码后与原已认证 Lease PID 比较 |
| `git_stdout_eof`、`git_returncode` | 完整唯一预期 OID 行且 Git 正常返回0，不接受捕获前缀 |

握手上限64KiB；生产者证明上限4096字节。根过程双流捕获仍沿原受控 IO，内层 stdout 只允许唯一
完整 OID 与 LF。原普通输入1MiB、普通命令每流1MiB均不变。不能把这些控制限制解释为业务材料降到1MiB。

## 7. 正常流程、时序与完整说明

```mermaid
sequenceDiagram
    participant H as 受信宿主
    participant O as 原Supervisor/Owner
    participant W as 固定根子程序
    participant G as 固定Git子进程
    H->>H: 准备用途和原Plan批准
    H->>O: start 原Plan/Spec/Capability
    O->>O: 校验并冻结保护集合
    O->>W: 在原进程树启动，stdin等待握手
    O-->>H: 返回实际根句柄
    H->>H: 同一集合检查完整body并私有落盘
    H->>W: 原stdin发送小manifest并关闭
    W->>W: 完整EOF/目标/配置/长度/SHA/OID核验
    W->>W: 创建完整RO regular并关闭全部写端
    W->>G: 固定argv/env，stdin为完整RO句柄
    G-->>W: 唯一完整OID行、正常退出
    W->>W: 复读RO及目标绑定
    W-->>O: 小型生产者证明与完整双流终态
    O-->>H: 原V2 MAC/raw/EOF/Lease
    H->>H: 核验双流、保护、字段及根PID，清理自有stage
    H->>O: 独立新Plan批准完整对象回读
```

根子程序必须观察小 stdin EOF 才会处理请求，不提前执行 Git。宿主输入保护命中时不落 stage、不发 manifest，
只停止并结算等待中的原根进程。保护检查和 staging 在线程执行，直接 Task 取消也先取消原令牌并排空该线程。

POSIX 快照先排他建空文件、开 RO 端并去名，随后才写任何正文；写满、fsync、关闭写端后复读 RO。
Windows 从 `CREATE_NEW` 设置删除关闭与拒共享写/删除，写满后显式降权复制只读句柄，关闭写端后复读。
两个实现都在 Git 前取得完整普通文件，不以正文管道模拟只读快照。

## 8. 数据流与信任边界

```mermaid
flowchart TD
    M[模型与外部输入 不构成来源授权] --> P[原发布Patch/CAS来源 待产品桥接核验]
    P --> B[受信完整bytes及source digest]
    S[Owner校验的冻结保护集合] --> C[宿主完整保护谓词]
    B --> C
    C --> F[私有stage完整bytes]
    A[原Plan绑定用途及manifest摘要] --> W[安装包内受信worker]
    F --> W
    W --> R[完整RO snapshot]
    R --> G[固定对象写入]
    G --> Q[worker过程证明]
    Q --> V[Owner raw MAC后宿主核对]
    V --> I[独立批准的对象读取与OID校验]
    I --> D[待接线的业务账本及备份闭包]
```

保护集合不传给 Git，也不出现在 manifest/证明；子程序通过完整 body SHA、OID 和受信代码继承宿主已核验的
保护谓词。这是受信生产者机制，不是 Owner 直接观察 Git stdin 的声明。
公开脱敏输出不能替代完整私有正文或原 raw 认证，正文及个人路径不进入公开验证包。

## 9. 核心业务逻辑伪代码

```text
prepare:
    验证原仓库实体绑定、Git实体、完整类型/长度/OID和同一预算
    固定内层命令、私有body名称、小manifest和基础解释器入口
    返回待批准材料，不创建body或启动过程
run:
    重验全部材料、原Plan/批准/环境/能力/Workspace
    原Owner启动固定子程序并冻结保护集合
    检查完整body；完整私有stage；向原stdin发送小manifest并close
worker:
    完整读取manifest到EOF；严格字段和canonical校验
    核验目标和控制闭包；完整读取stage，核对长度/SHA/OID
    建立从正文写入前开始的快照死亡生命周期
    写满并关闭所有写端；完整RO复读
    同一进程树启动固定Git，接受唯一OID行及0退出
    复验snapshot和目标，生成小型证明
settle:
    原Lease/MAC/raw/EOF完整核对；保护集合不得改写结果
    校验证明全部绑定和根PID；仅清理同一实体自有stage
    任何握手可能送达后的不完整结果 -> material effect unknown，不重放
    该用途标志覆盖原Supervisor整个退出；其关闭异常不能把强错误降级
    Supervisor退出后清理自有stage；实体变化保持强错误，不删除陌生文件
    新批准的完整对象回读后，才允许后继业务登记判断对象后置条件
```

## 10. 异常、取消、超时与恢复

| 阶段/故障 | 语义与动作 |
|---|---|
| 无批准、用途/目标/程序漂移、原保护来源缺失 | Owner 和 stage 创建前拒绝；不执行 Git |
| 完整输入保护命中 | 原 Owner 已启动，但无 stage/manifest/Git；停止并验真原根终态 |
| staging 取消或期限耗尽 | 先排空线程和原根句柄；仅清理 O_EXCL 创建且实体匹配的文件 |
| manifest 可能送达后退出、取消、超时、损坏证明、PID不匹配 | `git_material_effect_unknown`；对象可能存在，禁止自动重试、删除对象或回退 Ref |
| 原 Owner 失联/损坏/unknown | 原强错误及业务不确定性不能被普通取消或超时覆盖；Lease真实终态不伪装为另一个状态 |
| 原 Supervisor 退出再次抛出控制失联错误 | 用途标志保留至整个 `async with` 结束；可能送达后的任何退出异常仍归类为 `git_material_effect_unknown` |
| stage 清理身份变化或清理失败 | 强失败，不返回过程成功；不按 glob 删除陌生文件 |
| 宿主硬崩溃 | 不靠 finally 声称全部清理；body stage 的耐久归属/恢复由后继业务阶段账本闭合 |

快照的首次真实正文写入后死亡屏障分别验证取消、强杀和超时，不代表排他重放每个历史调度。
原控制写失联存在两层关闭：内部停止/等待和 `Supervisor.__aexit__` 再次关闭原句柄。
仅在内部 helper 保存强错误不完整，后者仍可能以 `process_not_owned` 覆盖该错误。
宿主在进入 Supervisor 前建立 `input_sent` 与自有 staging 引用；在每次原控制发送之前标记可能送达。
最终错误分类覆盖整个 Supervisor 退出，私有实体清理在此边界之外完成。
真实进程故障回归保留内部及外层实际关闭失败，没有吞掉故障、修改原 Lease 或自动重发材料。
POSIX 创建空名称至去名之间仍存在短窗口；不宣称抵御此窗口的任意硬退出，也不把空文件残留等同于
批准正文的前缀对象。正式业务阶段登记与崩溃扫描仍必须补齐，不能因此提前开放默认 Git 写 Tool。

## 11. 持久化、可观测性、安全与部署

沿原 ExecutionPlan SQLite 和 Process Lease/Owner receipt 保留意图及实际过程；不增加数据库格式或密钥。
阶段目录为私有权限，代码正文隐藏于 repr，失败仅公开固定错误码。原 Plan 中的受控路径/参数属于私有状态，
不复制到公开报告。记录 process ID、用途摘要、输入长度/SHA、失败阶段和实际 Lease，不记录保护值或正文。

子程序仅依赖 stdlib，以固定安装根导入，`-I` 隔离 cwd/PYTHONPATH，避免工作区伪装包入口；
四模块及两个 `__init__` 进入实现摘要。基础解释器和 Git 均另有实体身份。
新快照读端没有写/追加权限；Windows Job 与原 POSIX Owner 回收整个原进程树。
路径、ACL、无 hardlink/reparse、配置闭包和有界对象命名空间检查仍保留，不自动 fetch 对象或执行过滤器。

宿主和子程序共用原绝对单调期限；原 Owner 的实际启动期限可以更严格。
不把传输到子程序的预算值宣称为 Owner 内部实际 deadline 的同一数值。
默认 CLI、SDK/Protocol、MCP 和 Provider 注册均不新增 Git 写入口，安装后只增加内部实现文件。

## 12. 完整测试、验证范围与发布条件

| 测试文件 | 覆盖 |
|---|---|
| [完整输入](../../tests/product_config/test_git_material_input.py) | 三类型×两格式×完整8MiB/合法最小；真实Owner/Git写入、新批准回读；保护、批准、严格JSON、身份/用途漂移、线程取消/期限和不确定证明 |
| [快照死亡生命周期](../../tests/product_config/test_git_material_snapshot_lifecycle.py) | 实际根PID和第一次真实快照写入屏障；取消/强杀/超时，原MAC和进程回收；不执行Git，不冒充写业务成功 |
| [完整原监督器退出](../../tests/product_config/test_git_material_owner_exit.py) | 真实 Owner 控制写 OSError、内部停止及外层关闭均失败；普通错误/调用取消/期限耗尽保持未知效果；清理前实际更换实体，分别验证未送达拒绝及已取得证明仍不返回成功 |
| [配置与原生保护](../../tests/product_config/test_git_material_native.py) | Git配置引号/注释解释及禁止输入；实际SHA256对象写入；Windows属性句柄负对照与数据读句柄拒共享写/重命名/删除，单机跳过不算原生通过 |
| [原对象读取](../../tests/product_config/test_git_object_material.py) | 原209案例及后继四负例保持，完整OID/EOF/容量/保护读取合同 |
| [原受控IO](../../tests/product_config/test_git_delivery_process.py) | 原62案例、1MiB、计划批准、PID、进程树、取消和raw回执保持 |

同候选[验证包](../validation/git-material-input-2026-10-01-v1/README.md)固定1232件代码输入，
关联1014通过/43跳过；同一实际Wheel的419模块与源码及两处安装逐字节一致，
源码外Python3.12/3.13各356通过/2跳过。原控制失联缺陷3失败与后继回归分开保留，
五项真实Owner/实体清理案例覆盖整个退出边界，独立复核确认该发现静态闭合。
不将单机通过推导为消费者Windows11、Linux/macOS全部发行输入或完整商用门禁。
正式接线仍需来源桥接、完整 Diff 与独立批准、双受管工作树、耐久材料和原认证账本、阶段恢复、Backup v2；
R3完整20 Trial、独立 Beta 和同候选 R1～R6均继续开放。测试集合重叠不相加，历史失败不覆盖。


## 13. 快照流封装的FD归属与失败清理

### 13.1 需求背景、已确认缺陷与设计目标

原[`_snapshot`](../../src/harnessix/delivery/git_material_native.py)以
`os.fdopen(os.dup(descriptor), "rb", buffering=0)`建立Git标准输入流。复制成功与FileIO封装成功
是两个不同的资源边界；真实Python审计钩子在`open(integer_fd, ...)`处抛出异常时，
新FD没有进入原资源栈，资源退出后仍可读取匿名快照。macOS真实反例已确认此失败清理缺陷，
没有mock `dup/fdopen`，不要求成功启动Git才能触发。

目标是让复制出的FD从流构造前即有明确Owner；流封装的普通异常与控制异常都不能留下该FD。
正常路径保留原RO、普通文件、完整字节、归零、同一文件对象和退出顺序。
此缺陷发生于Popen之前，与既有Windows首失败`git_popen_returned=true`不相容，
**不是Windows Git128根因或原生修复证据**；不更改Windows共享标志、降权、审批或业务成功门。

### 13.2 总体架构、源码位置与职责

```mermaid
flowchart TD
    A[原snapshot已写满且复读归零] --> B[os.dup复制只读FD]
    B --> C[原Resources登记os.close]
    C --> D[fdopen closefd=false构造流]
    D -->|成功| E[Resources登记stream.close]
    D -->|异常| F[原异常传播并退出Resources]
    E --> G[原Git执行及原证明流程]
    G --> H[关闭流再关闭复制FD]
    F --> I[关闭复制FD及既有资源]
```

| 类／函数／字段 | 职责、契约与来源 | 生命周期 |
|---|---|---|
| `_Resources` | 原ExitStack子类；持有本次目录、FD和流，不新增Owner | 原Worker作用域 |
| `_snapshot` | 原正文和OID验真、RO快照生成、归零及受管流构造 | Git启动前执行 |
| `descriptor` | 原已完整复读且归零的RO FD | 原Resources已有归属 |
| `stream_descriptor` | `os.dup`实际返回的同文件对象FD，不按路径重开 | 立即登记到原Resources |
| `closefd=False` | FileIO负责流状态，FD由资源栈独占结算；避免封装异常出现无主FD | 不改变底层访问权利 |
| `stream.close` | 在FD关闭前关闭流；调用方提前关闭流不会提前释放栈持有的FD | LIFO先流后FD |

本变更只涉及[`git_material_native.py`](../../src/harnessix/delivery/git_material_native.py)的末端封装，
不改变输入模型、数据库、manifest、材料Proof或公开错误Schema。
[`新失败回归`](../../tests/product_config/test_git_material_stream_ownership.py)使用原材料夹具和真实基础解释器，
与原Owner死亡屏障和完整Git输入测试分工，不代替后两者。

### 13.3 正常与失败流程、时序与数据流

```mermaid
sequenceDiagram
    participant W as 原Worker
    participant S as snapshot
    participant R as 原Resources
    participant F as FileIO
    W->>S: 原材料验真并取得RO FD
    S->>S: dup并登记FD关闭回调
    S->>F: fdopen closefd=false
    alt 封装成功
        F-->>S: stream
        S->>R: 登记stream.close
        S-->>W: 原完整输入流
        W->>R: 原作用域退出
        R->>F: stream.close
        R->>S: os.close复制FD
    else 审计或控制异常
        F-->>W: 原异常传播
        W->>R: 原作用域退出
        R->>S: os.close已登记FD
    end
```

```mermaid
flowchart LR
    B[原正文及完整摘要] --> R[已复读RO快照FD]
    R --> D[同文件对象dup FD]
    D --> F[不拥有FD的只读FileIO]
    F --> G[原Git标准输入]
    O[原Resources] -. 独占FD关闭归属 .-> D
    O -. 流先于FD结算 .-> F
```

正文不进入审计结果、日志或公开报告；只是原匿名／DELETE_ON_CLOSE快照的同对象引用，
不新增副本文件或耐久存储。共享文件位置仍由原构造前归零保证，没有独立offset或新PIPE。

### 13.4 核心逻辑、失败语义与恢复边界

```text
原完整RO快照已验真并归零
复制原descriptor为stream_descriptor
立即登记resources.callback(os.close, stream_descriptor)
构造fdopen(stream_descriptor, rb, buffering=0, closefd=false)
登记resources.callback(stream.close)
返回原用途输入流
封装抛错时：原异常传播，原作用域关闭已登记的FD
正常退出时：先关闭流，再关闭FD，再结算更早登记的原资源
```

采用`closefd=False`而不是失败后盲目再close：流与FD的关闭责任清晰，
不依赖FileIO在不同失败点是否已接管或关闭FD，也不增加双重关闭或忽略EBADF的补丁。
原作用域提前关闭流后，复制FD继续由资源栈持有直至作用域退出；该流是内部借用对象，
不构成新的公开FD转交契约。取消、KeyboardInterrupt与普通异常复用同一LIFO清理，
不新增线程、重试或持久恢复。进程硬退出仍依赖OS关闭句柄和原快照去名语义，
不能用本次finally路径替代原真实死亡屏障。

### 13.5 认证实现、固定输入、兼容与部署

native源码属于原安装实现摘要，新Plan必须按实际新字节重新规划并批准；不继承旧批准。
Windows固定输入名单保持十八件，仅更新native源码一行的完整LF／CRLF大小和SHA，
合同读器及整体身份回归同步绑定新合同字节。其他十七件、PE/PDB、13hook、两selector、
20/45/240/300秒期限、原branch/proof/SDK门和旧失败产物均不改。
只改变安装包内部实现，无数据迁移或新配置；离线通过不宣告Windows现场或1.0通过。

### 13.6 完整验证矩阵与未完成边界

| 场景 | 验证要求 |
|---|---|
| 真实封装审计拒绝 | 不mock fdopen/dup，原异常保留，退出后复制FD为EBADF |
| 真实控制异常 | KeyboardInterrupt同样结算，不能被关闭异常覆盖 |
| 连续拒绝 | 每次失败均无存活复制FD和命名快照，不能靠子进程最终退出掩盖泄漏 |
| 正常实际RO流 | 起始offset0、完整二进制字节、拒绝写入、原作用域退出后FD关闭 |
| 原Owner死亡与材料写入 | 原取消／超时／强杀、8MiB／三类型／两格式及独立回读继续回归 |
| Windows固定输入与有限门 | 新十八件完整身份可验，旧门不放宽，历史原生FAIL保持 |

RED、修复后结果、精确来源和独立复核须分阶段保存。真实Windows流故障和原两SDK
成功门仍需现场证据；完整Git业务交付、Backup v2、R3真实质量、Beta与R1～R6均继续开放。
