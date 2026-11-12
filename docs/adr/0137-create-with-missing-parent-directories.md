---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [delivery, workspace, trusted-actions, product-ui]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0093-kernel-owned-public-failure-contract.md
  - docs/adr/0129-file-tool-leniency-and-correctable-rejections.md
related_tests:
  - tests/workspace/test_snapshot_missing_parents.py
  - tests/workspace/test_snapshot.py
  - tests/delivery/test_create_missing_parents.py
  - tests/delivery/test_path_case_aliases.py
  - tests/product_config/test_patch_missing_parents.py
  - tests/product_config/test_compact_workspace_patch.py
  - tests/product_ui/test_diff_view.py
  - tests/product_ui/test_tool_summary.py
supersedes: []
---

# ADR 0137 新建文件时由事务补建缺失的父目录

- 状态：评审中
- 日期：2026-11-11

## 1. 背景

`apply_patch_batch`在不存在的目录里新建文件时整批失败：`workspace_parent_missing`「缺失资源的父目录不存在」。
补丁工具没有创建目录的操作，工具说明没有写这条限制，报错没有给出下一步；批次是原子的，同批里其他
合法修改也一起不生效。

- 真实用户（PyPI 0.10.0b12，macOS，两轮Beta）：7次，出现在5个需要新目录的任务中的3个。`state-ocr`会话
  事件569（同批4项，其中一项新建`tests/`下的文件）、583（单独新建空的`tests/__init__.py`）、765连续失败，
  直到测试者让模型先执行`mkdir -p tests`，从首次失败到解决约5分钟。
- 评测（第二轮dev基线，60个试次）：2次，gin-gonic__gin-3227的xCdYk5j与WoR7i6W（事件流第162、911行）：
  模型为复现程序新建`scratch_repro/main.go`、`_scratch/main.go`，Go的`main`包必须有自己的目录。
- 同一位置还有一个更重的缺陷：父路径上已有的段是普通文件时（如`a.txt/b.py`），规划期抛出的
  `workspace_wrong_file_type`不在可回传清单里，Turn以`uncertain_effect`中断，模型没有改正的机会。

原因在事务的三处假设，而不只是快照里的一行报错：

1. 快照观察缺失资源时要求父目录已存在，缺失资源的身份绑定“父目录身份＋名字”
   （`workspace/snapshot.py`；Windows为`workspace/windows.py`）；
2. 发布成员时逐段打开父目录，缺一段就失败；恢复时观察“父目录不存在的成员”得到的是观察失败而不是“不存在”
   （`delivery/filesystem.py`）；
3. 计划合同只有文件成员，没有目录。

## 2. 决策

新建文件（成员的before为`absent`）时，事务补建工作区内缺失的父目录，属于同一次审批、同一笔事务。
不新增目录操作、不改任何公共合同：目录不成为事务成员，“这笔事务补建哪些目录”从已保存的计划推导。

### 2.1 规划：缺失的父目录如实进入快照

- 快照中**写目标及其全部祖先**可以位于尚不存在的目录之下，观察结果为`missing`；读取与执行请求不变，
  父目录不存在仍是`workspace_parent_missing`。放行范围由请求集合推导（`creatable_resource_keys`），
  捕获与复核用同一规则，无需新增参数或字段。
- 身份：缺失资源绑定**最近的已有目录**的稳定身份，加上其下自首个缺失段起的路径。父目录存在时与原来
  逐字段相同（原观察路径未改），已保存的计划不会因升级而失效。
- 已有路径段的校验不变，且先于“缺失”的判定：段是符号链接→`workspace_path_denied`，是普通文件等非目录
  →`workspace_wrong_file_type`；`..`、绝对路径、受保护与敏感名称照旧拒绝。每个缺失段的名称都经过
  同一路径策略（规范化、受保护组件、敏感名称）。
- 两代快照都支持：第1代把父目录作为显式`read`资源（`kind=missing`），第2代写入父目录历史
  （`WorkspaceParentObservation`原本就允许`missing`）。
- 批准之后目录出现（无论是目录、文件还是符号链接）时，执行前的快照复核失败，事务不发布。

### 2.2 发布：补建、失败即清理、绝不跟随符号链接

`filesystem._parent(create_missing=True)`只用于新建文件的成员：

- 缺失段用`mkdir(dir_fd=父目录)`补建并`fsync`父目录。`mkdir`不跟随符号链接（名字上已有任何东西都失败）；
  补建后的段与已有段走同一条检查：`lstat`类型、`O_NOFOLLOW|O_DIRECTORY`打开、同设备、身份一致。
- 权限：`0755`，仍受进程umask收窄（umask 077时为`0700`）。不在创建后`chmod`，避免对一个可能已被
  替换的名字改权限。
- 成员在把文件换入之前失败（写临时文件失败、目标漂移、存储不可用）时，由深到浅`rmdir`本次补建的目录。
  `rmdir`只删除空目录：原有目录、已有其他内容的目录都不会被删。工作区与这个成员开始前相同。
- 恢复只观察的约定不变（`reconcile_writes: False`）：父目录不存在的成员观察为`absent`
  （原来是`delivery_observation_failed`），于是“全部before”“after前缀”等判定照常成立。

### 2.3 批次的原子性

| 何时失败或取消 | 目录 | 工作区 |
|---|---|---|
| 规划期（任一项参数、SHA、锚点、路径被拒） | 不创建 | 不变 |
| 用户拒绝审批，或批准前取消 | 不创建 | 不变 |
| 执行前快照复核失败（工作区在批准后变化） | 不创建 | 不变 |
| 新建成员自身在换入文件前失败 | 本次补建的已删除 | 与该成员开始前相同 |
| 更靠后的成员失败，或成员之间取消 | 保留（里面有已发布的文件） | 已发布的前缀保留并如实记账（`interrupted`/`diverged`），与本ADR之前相同 |

最后一行不是本ADR引入的：Workspace事务逐成员发布，中途失败留下可对账的前缀而不自动回退
（[ADR 0068](0068-transactional-workspace-and-git-delivery.md)）。本ADR只保证目录不比文件多留一步。

### 2.4 回滚

`rollback_workspace_patch`发布逆向事务（删除新建的文件）之后，在仍持有Workspace租约时删除原Patch
补建、此刻为空的目录（`WorkspaceTransactionActionExecutor(after_publish=…)`）。清单来自**原计划**的来源快照：
规划时观察为缺失、且位于某个新建文件之上的父目录（`parent_directories.planned_new_directories`）。

- 原本就存在的目录不在清单里，回滚后即使为空也保留；
- 清单里的目录若已有别的内容（用户之后放进去的文件），`rmdir`失败即保留；
- 清理是尽力而为，不改变回滚的成功结果。
- 回滚一次删除时逆向事务要重新建出文件：其目录若已不存在，同样按2.2补建。

### 2.5 审批与结果如实展示

审批Diff与成功结果的`diff_preview`在全部文件段之前为每个要补建的目录写一行`new directory <路径>`
（与`new file mode`、`preserved mode`同类的说明行）。它由计划确定性推导，审批时与成功后重建的摘要一致；
不涉及新目录的计划，Diff字节与原来完全相同。终端界面把这些行并入文件清单，显示为「新建目录  路径」。
回滚的逆向Diff不列出将删除的目录（见第4节）。

### 2.6 同批路径冲突

目录可以由事务补建之后，两类原来被“父目录不存在”挡住的批次会走到发布阶段并留下部分效果，改为
规划期整批拒绝（`delivery_path_case_conflict`，未执行的可改正失败）：

- 一个路径既是新建文件、又是另一项要补建的目录（`pkg`与`pkg/a.py`）；
- 不区分大小写的文件系统上，尚不存在的目录下的别名（`pkg/a.py`与`pkg/A.py`、`pkg/a.py`与`PKG/a.py`、
  `Pkg`与`pkg/a.py`）。`path_case`原来在遇到不存在的父目录时跳过，现在把余下的整段路径按最近已有
  目录的大小写语义比较。

### 2.7 给模型的说明

| 情形 | 结果 |
|---|---|
| 父目录不存在（POSIX） | 成功 |
| 父路径上已有的段是普通文件等非目录 | `workspace_wrong_file_type`，未执行的失败：说明该段不是目录、缺失的目录会自动创建；原来中断Turn |
| 父路径上已有的段是符号链接 | `workspace_path_denied`，原说明补上“或经过符号链接的路径” |
| 父目录不存在（不补建的平台，即Windows） | `workspace_parent_missing`，改为英文：该平台不创建目录，先用命令工具`mkdir`再原样重发 |

界面按错误码保留中文原因（`_USER_REASONS`）。工具描述不变：原文没有写过“父目录必须存在”，现在也没有
这条限制；描述进入工具指纹，改它会使升级前已批准未执行的调用按“合同已变化”被拒。

## 3. 不变的部分

- **公共合同与Schema：** `WorkspacePatchInput`、`WorkspaceMutation`、`WorkspaceTransactionPlan`、两代快照、
  父目录历史、Review记录、事件、`spec/`与产品配置均未修改；没有新的账本表或迁移。
- **路径安全：** 仍只有逐段no-follow一条打开路径；新增的系统调用只有`mkdir`与`rmdir`（都带`dir_fd`、
  都不跟随链接）。
- **审批绑定：** 计划指纹、快照Revision与Review摘要的算法不变；新目录通过快照里的`missing`事实
  进入被批准的内容。
- **只观察恢复：** `reconcile`仍不写工作区。
- **Windows：** 事务端口不补建父目录，行为与之前相同（只有上表的说明文字变化）。Windows的观察与写入
  经原生句柄实现，本次无法在可运行的环境里验证，未改。

## 4. 未做与残留

- **硬退出后的残留：** 进程在补建目录之后、换入文件之前被杀时，留下空目录（以及事务自己的
  `.harnessix-<事务>-<序号>-….tmp`，与在已有目录里硬退出时相同）。恢复不写入，所以不清理；这些目录
  位于工作区内、名字经过校验并已被批准、不含内容，再次执行同一补丁或恢复发布都能继续使用。
- **回滚逆向Diff不显示目录删除：** 逆向计划的摘要只能由该计划自身推导，原计划不在其中。可选做法：
  回滚执行器用原计划补一段说明并让审批摘要与成功摘要都走它，或在Review合同里加目录条目（合同变更）。
- **回滚清单以原计划为准：** 批准后、成员写入前由别人建出同名目录时，事务没有创建它，回滚仍会在它
  为空时删除。窗口限于一次发布之内，且只影响空目录。
- **底层`build_rollback`接口**（非产品工具）生成的逆向事务不清理目录。
- **Git交付的来源观察未改：** `git_delivery_source`以`read`请求观察各路径的最终版本，不在放行范围内。
  已被回滚（目录随之清理）的补丁若再被选作Git交付来源，原本以“目标已包含后续修改”拒绝，现在可能先以
  `workspace_parent_missing`拒绝；两者都不交付。
- **敏感名称的补丁路径**（`*.pem`、`*.key`、`.ssh`等，不在补丁的受保护组件表里）在快照观察处抛出未
  转换的读取拒绝并中断Turn。与父目录是否存在无关，本ADR之前即如此，未在此处理。
- **同批路径冲突的说明仍是中文**，与`path_case`的其他说明一致（[ADR 0129](0129-file-tool-leniency-and-correctable-rejections.md)第4节）。
- 不提供单独创建空目录的操作。

## 5. 后果

- 模型不再需要为新目录先执行一条`mkdir`命令；原来失败后重发整份补丁的步骤消失。
- 写目标的快照观察变宽：除补丁外，以写目标观察成员的`git_checkpoint.verify_checkpoint_worktree`对父目录
  不存在的成员现在得到`absent`而不是`workspace_parent_missing`。
- 父目录在事务中途消失的已有文件成员，由“观察失败”变为按第三事实结算（`diverged`）。
- 审批Diff对含新目录的补丁多出`new directory`行；解析该文本的外部工具需要容忍这类说明行。

## 6. 验证

- 快照：`tests/workspace/test_snapshot_missing_parents.py`（两代的缺失记录与复核、目录出现后失效、身份
  公式、文件／符号链接／越界段的拒绝、读取请求不放行）。
- 事务：`tests/delivery/test_create_missing_parents.py`（补建与权限、成员失败后的清理与原有空目录保留、
  靠后成员失败的既有语义、符号链接与普通文件顶替、成员观察为`absent`、真实进程硬退出后的对账与续发、
  计划清单与Diff、清理函数）。
- 路径冲突：`tests/delivery/test_path_case_aliases.py`（不区分大小写与区分大小写的卷各运行一次）。
- 产品路径：`tests/product_config/test_patch_missing_parents.py`（Beta回归、审批内容、整批拒绝与用户拒绝、
  回滚、非目录父路径、同批冲突；第1代与第2代快照装配各一遍）。
- 界面：`tests/product_ui/test_diff_view.py`、`tests/product_ui/test_tool_summary.py`。
- 评测中观察：`workspace_parent_missing`计数应为0；新建文件前的`mkdir`命令步数。
