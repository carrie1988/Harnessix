---
doc_type: adr
status: current
version: 2
code_revision: 386dd2c30c86865529d9163afa27344c15462ba9
owners:
  - core
modules:
  - delivery
  - workspace
related_adrs: []
related_tests:
  - tests/delivery
  - tests/workspace
  - tests/delivery/test_preserved_file_modes.py
  - tests/delivery/test_path_case_aliases.py
  - tests/product_config/test_patch_precondition_rejection.py
supersedes: []
---

# ADR 0068：事务性 Workspace 与 Git 交付

- 状态：已接受
- 日期：2026-09-08；第2版 2026-11-08（缺陷纠正：保留现有文件的真实权限）

## 背景

现有受管 Patch 副本按成员顺序写入，可正确报告部分效果，但不是通用 Workspace 事务；Git Eval 交付也不是产品运行时。生产交付需要来源保护、完整 Diff、Checkpoint、Rollback、Branch/Worktree、Commit 和独立 Push。

## 决策

1. 新增 `WorkspaceTransaction`，私有账本持久化来源 Snapshot、文件 CAS、目标 manifest、批准 fingerprint、阶段和逐成员观察。
2. 修改先发生在受管 staging Workspace；Source 保持不变。冻结时生成完整新增/修改/删除/重命名 Diff 和 before/after CAS。
3. 发布前重新核对 Source Snapshot 与涉及文件身份；脏工作区默认拒绝覆盖，只有 manifest 明确包含且获得新批准的文件可变更。
4. 文件系统发布采用 write-ahead journal、同目录临时对象、fsync、逐成员 replace 与持久游标；跨文件不虚假宣称内核原子性，但任意崩溃点都能恢复为 before、after、diverged 或 unknown。
5. Git 仓库优先在独立 worktree/branch 中完成修改；Checkpoint 使用 Git tree/commit 或 CAS manifest。Rollback 是新的显式事务，不删除用户未纳入 manifest 的修改。
6. Commit 仅作用于计划列出的路径并显式绑定作者、消息、parent和预期tree。0.7默认禁用仓库Hook、fsmonitor、attributes外部来源及可执行filter；未来启用任何Hook/filter都必须作为新的可执行资源进入Sandbox和批准指纹，不能继承普通Commit许可。
7. Push 是 Action Plane 中独立的外部网络写，默认关闭；Commit 的批准不能授权 Push、force push 或创建 PR。
8. POSIX普通Workspace提供直接发布；Windows 0.7交付优先在受管Git worktree/branch中完成。未实现抗Reparse Point竞态的Windows普通目录发布端口前，该组合失败关闭，不以字符串复核冒充安全写入。
9. Commit使用确定性原始Git对象：Checkpoint先绑定tree和parent，Commit Spec再绑定作者、时区时间、消息、实现摘要与预期OID；执行只写入该对象，并通过旧值全零的`update-ref`创建此前不存在的新branch。该策略不修改来源HEAD/index，不运行Hook，也能在返回丢失后按对象正文与ref对账。

## 取舍

- 普通文件系统无法提供多路径单系统调用原子提交，因此承诺 durable recoverability，不使用“原子多文件写”误导用户。
- Git tree 是内容快照，不是对工作区、索引、submodule、LFS 和 Hooks 的完整事务；这些能力分别建模。

## 失败语义

- Source 漂移/脏冲突：`delivery_source_changed` / `delivery_dirty_conflict`；
- 写前失败：`not_applied`；
- replace 后记账前：`unknown`，必须 reconcile；
- 部分发布：`interrupted`，重开后按 manifest 核对，不盲目继续；
- Commit对象或ref更新结果丢失：按预期OID、完整对象正文和ref查询，不生成第二个commit；
- Push 结果丢失：按远端 ref 对账，不自动重推。

## 第2版：保留现有文件的真实权限（缺陷纠正，2026-11-08）

### 缺陷

第1版用Git能表示的两种权限（0644/0755）记录文件版本（`WorkspaceFileVersion.mode`）。原意是让每条记录
都能一对一变成Git提交，并避免“记录是一个权限、磁盘是另一个权限”。但这把“Git能表示的权限”当成了
“文件的真实权限”：Git只记录属主可执行位，0664、0777、0600等文件照样能提交；在磁盘上，它们与0644不同。
结果是只要现有文件不是0644/0755，补丁一律拒绝修改。常见触发场景：

- Linux默认umask为002的用户（如Ubuntu的私有用户组），`git clone`得到的文件都是0664；
- WSL在`/mnt/c`下、部分Docker挂载目录与官方评测镜像（E0c的django-11133、sympy-13031）中文件为0777。

用户反馈后定性为缺陷，优先修复。评测侧此前的权限规整（ADR 0120第7.1节）是临时绕行，本版是根治。

### 决策

1. 新增保留权限代际：`WorkspaceFileVersionV2`记录任意普通权限位（0～0777）；带setuid、setgid、sticky位的
   文件仍以`delivery_metadata_unsupported`拒绝，且仍是模型可修正的失败结果、工作区不变。
   对应`WorkspaceMutationV2`，两条Snapshot路线各一个Plan版本：`workspace-transaction-plan/v3`承接原Snapshot，
   `/v4`承接Snapshot v2；领域记录`record/v3`、`record/v4`，物理壳`workspace-stored-record/v4`，
   Diff文档`workspace-diff/v2`，Review首记录`workspace-action-review/v2`（代码见
   `delivery/preserved_mode_contracts.py`）。
2. 按需启用：规划时逐项按真实权限记录成员（`delivery/file_modes.py`），原逐项fail-fast顺序不变；只有事务
   涉及0644/0755以外的权限时才生成新代际，且整笔事务统一为新代际；其余事务无损转换为第1版的v1/v2，
   字段、字节和指纹都不变，
   已提交的合同Schema、冻结的Git交付合同和Windows证据绑定的源码都不受影响。
3. 权限语义：`edit`保留现有文件的真实权限；`replace`、`create`仍使用模型给出的0644/0755（模型可见的输入
   合同不变）；回滚恢复原权限；发布、复核与崩溃恢复对账都按真实权限比较（观察结果总是真实权限，
   与任一代际的记录按字段比较）。由此带来一项行为变化，用户已确认（2026-11-08）：原代际事务发布中途，
   若文件权限被改成0644/0755以外，此前报`delivery_metadata_unsupported`，现在判为来源漂移
   （`delivery_source_changed`，记录转为diverged），与内容被改时的处理一致。
4. 合同守卫：原代际的Mutation与Plan拒绝夹带新代际成员（子类实例也拒绝），避免把0664写进原合同。
5. 存储与降级：保存新代际记录时，账本在原业务SQL事务内升级为Schema4；只认识Schema1～3的旧程序打开
   该账本时失败关闭（`delivery_store_version`），不会把0664误读为0644。状态备份接受Schema4。
   用户已决定（2026-11-08）接受这一降级影响：账本一旦含新代际记录，旧版本整本拒绝打开，原有事务也随之
   不可读，而不是部分读取。
6. Git交付暂不接受新代际：`product_config/git_delivery_source.py`在读入原Patch后立即以
   `git_delivery_source_mode_unsupported`拒绝并说明原因，不进入`delivery/git.py`。Git本身能提交这些文件
   （按属主可执行位记为100644/100755）；以后为Git交付单独升版时按这一规则映射，需重跑Windows CI。
7. 审阅：Diff在权限改变时显示`old mode`/`new mode`，在保留0644/0755以外的权限时显示`preserved mode`。

### 被否决的做法

- 就地放宽原类型的取值范围：会改变已冻结的Git交付合同与20个已发布Schema，也要改动Windows证据绑定的源码；
- 所有新事务都用新代际：改动面最大，并让Git交付对全部新事务失效；
- 写回时把权限规整为0644/0755：会悄悄改变用户的文件权限，回滚也无法恢复原权限。

### 相关风险：其他把Git语义当成磁盘状态的地方（2026-11-08排查，只记录不修改）

| 位置 | 现象 | 影响 | 是否需要修 |
|---|---|---|---|
| 符号链接、硬链接（`tools/workspace.py`的`_check_type`） | 补丁目标是符号链接或硬链接计数大于1时拒绝（`path_denied`） | 失败关闭，不会丢失链接语义 | 否；如需支持另行设计 |
| Windows可执行位（`planner._normalized_targets`、Windows端口） | Windows只记录逻辑0644，拒绝0755目标 | NTFS无POSIX权限，记录与磁盘一致 | 否 |
| 原子替换丢失的其他文件属性（`filesystem._apply`的临时文件+`os.replace`） | 新文件的属主为当前进程用户；扩展属性、ACL、macOS标签等不随替换保留；权限已按本版保留 | 以root运行修改他人文件时属主变为root；依赖xattr/ACL的文件会丢失这些属性 | 已决定：开源后修复（用户2026-11-08） |
| 大小写不敏感的文件系统（`workspace/paths.py`：POSIX路径按大小写敏感比较） | macOS默认APFS大小写不敏感；实测已有`a.txt`时，补丁写`A.txt`被规划为修改该文件，同一批同时写`a.txt`与`A.txt`会规划成两个成员 | 第二个成员发布时发现文件已变而转为分歧，留下部分效果；路径大小写与Git索引不一致 | 已决定：开源前修复（用户2026-11-08），见下节 |
| 换行符（Workspace写入原始字节；Git交付使用`core.autocrlf=false`的原始对象） | 不做换行转换 | 与`git add`在启用`text`/`eol`属性时的规范化结果可能不同 | 否（与第1版“禁用attributes外部来源”的决定一致）；Git交付升版时一并说明 |

### 大小写不敏感文件系统上的路径别名（2026-11-08修复）

缺陷：POSIX路径按字节比较，而macOS默认APFS不区分大小写（通常也不区分Unicode规范形式）。已有`a.txt`时，
写`A.txt`的补丁被规划为修改该文件，Diff、审阅和Git看到的路径都与磁盘不一致；同一批同时写`a.txt`与
`A.txt`被规划成两个成员，发布到第二项时发现文件已被第一项改变而转为分歧，留下部分效果。

决策（`delivery/path_case.py`）：

1. 规划前按文件系统的实际语义识别别名，只读取目录项与元数据：路径的每一段都必须与磁盘上的目录项
   完全一致；某段不在目录项中却能打开，说明它是已有名字的别名，整批以`delivery_path_case_conflict`
   拒绝，并在原因中给出磁盘上的真实路径，已有文件的名称以磁盘为准。
2. 同一批里两项若在所在目录的语义下是同一名字（不区分大小写的目录按NFC加casefold比较），整批拒绝，
   提示合并为一项。目录是否区分大小写用该目录中已有名字的大小写变体探测，没有可用名字时用目录自身
   在父目录中的名字，都无法探测时按平台默认（macOS视为不区分，其他POSIX视为区分）。
3. 两个原规划入口（原Snapshot与Snapshot v2）都执行该检查；补丁规划在存在性判定之前执行，使模型收到
   “改用磁盘名称”而不是“目标已存在”。拒绝是可修正的失败结果，工作区与事务账本都不变。
4. 区分大小写的文件系统（一般的Linux）行为不变：`a.txt`与`A.txt`仍是两个文件。Windows仍按原有的
   大小写折叠比较键处理，本次未改动；Git交付读取来源的路径不经此检查。

