# ADR 0028：受管副本内的持久单文件 Patch 执行

- 日期：2026-09-03
- 状态：0.5.3b1 已实现并完成范围内验收；Kernel 写工具接入留到 0.5.3b2

## 1. 拆片依据

`b0622cb` 四项 CI 全绿。进一步核对发现现有 ApprovalRequestContent.policy_version 固定为 kernel-read-only/v1；其持久 Schema 和恢复前提不能原地改义。先交付不依赖 Agent Loop 的受管工作副本/写执行内核，再通过独立 Agent 契约升级接入模型工具。b1 不把 apply_patch 广告给模型，整体 0.5.3b 不勾选完成。

## 2. 工作副本而非源目录原地写

宿主在源目录外指定私有管理根，由工厂生成 UUID 子目录，包含 workspace、owner.lock 和私有 ledger.sqlite。不可把任意已有目录注册为可写副本。管理根/副本只允许当前 UID、0700 权限，锁文件和账本不允许链接；重开核对目录、锁、数据库及工作区身份。同副本以进程锁和同步互斥串行操作。

从受信 Workspace 导入宿主明确选择的相对路径，最多 256 个 UTF-8 普通单链接文件、单文件 1 MiB、合计 32 MiB；复用拒绝路径/no-follow/漂移检查，不读取 .git/.env，不运行 Git、钩子或仓库代码。只复制内容和普通权限位，不复制 ACL/xattr、链接或 Git 元数据。导入是逐文件观测，不声称整树同一时刻快照或完整 Git worktree。副本仅代表清单中的文件。

新副本先 building，完整文件/来源证据落库并刷盘后才 ready；失败的 building 副本不准执行，不自动递归删除未知目录。源目录始终只读，不自动合入修改；私有副本的管理权限不是 OS Sandbox，也不能防御同 UID 恶意进程。调用方不得把副本交给并发编辑器/不受管进程。

## 3. 持久计划和审批

复用 0.5.3a PreparedPatch、完整前后镜像与复核。每份副本独立账本绑定其生命周期，不强制本地文件执行通过 Action Worker/HTTP。保存私有计划后形成待审批项，request_id 幂等且不同载荷冲突；计划不可修改。每副本最多 64 个计划，计划前后镜像合计最多 32 MiB，来源基线另受导入预算限制；不是整个 SQLite/journal 的物理磁盘上限。

批准/拒绝复用现有 ApprovalDecision 值对象。审批指纹绑定副本 ID、计划 ID、request_id 和内容计划指纹；答复只持久化，不执行。actor 是受信宿主声明，不是新增远程身份认证。Kernel 的只读审批不会被悄悄当作本片写审批。

## 4. 一次性执行与观察

approved → started 的事务必须先于任何目标文件写入；该边界一经消费就不自动重复。临时后镜像位于模型不可读的副本元数据目录，排他创建、完整写入、设置批准权限并 fsync；其 dev/inode 证据先落库。再次核对源文件与父目录身份后，用目录 FD 定位执行单文件原子替换，再同步文件/源临时目录/目标目录，最后持久化结果。

文件与 SQLite 不是同一事务。替换尚未尝试的受控异常可记录 failed；替换尝试后不能证明完成的错误记录 uncertain。取消发生在替换前可停止；替换后先完成效果核对与记账，调用方在线程中运行时必须等待清理。不会因 Task 取消就让后台写继续而先报成功回滚。

重开不执行任何 Patch。宿主显式 reconcile 只读观察并追加证据：原前镜像/吻合后镜像/第三种内容/目标缺失/不可读取。只有持久临时 inode 与后镜像、权限共同吻合才标记 observed_after；仅后镜像字节相同而缺少归因证据仍是不确定。观察不是重新执行许可；即便 observed_before，要再次尝试也必须显式创建新计划并重新批准。

锁只协调受管宿主；复核+rename 仍不是对任意并发编辑器的内容 CAS。仅支持工厂创建的私有副本，拒绝工作区根/父目录交换、特殊权限位、链接、非当前 UID 文件和带用户 xattr/扩展 ACL 的目标（Darwin 私有新文件的 com.apple.provenance 系统标记除外，不承诺保留其值），不承诺通用元数据保留。

## 5. 验收与后续

真实副本修改而源文件不变、审批等待/拒绝/错绑、计划配额/幂等/损坏、重复执行拒绝、第二宿主、根/父/目标漂移、取消、短写/磁盘满、临时文件清理、各持久/替换切点进程退出及不重写恢复。默认仍无模型 API/服务器调用。

b2 再定义 Agent 持久写审批/结果契约、Scoped 工具准入、线程回收、未结算效果与 Kernel 恢复的组合；不得以 b1 的宿主 API 验收代替模型调用闭环。源目录合入、多文件部分效果和 Process 继续分片验证。


## 6. 跨平台核对与实测修正

Python 的 os.listxattr 不具备本项目所需的 macOS 可移植性。本片通过标准库 ctypes 调用已核对签名的 flistxattr：Linux 三个参数，Darwin 四个参数；仅按 FD 检查、不退回按路径读取。Darwin 另外使用 acl_get_fd_np(ACL_TYPE_EXTENDED) 检查扩展 ACL，并拒绝非零文件 flags。检查失败直接关闭写入口。[Python 扩展属性接口](https://docs.python.org/3.12/library/os.html#os.listxattr)、[Apple flistxattr](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/listxattr.2.html)、[Apple ACL FD 接口](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/acl_get_fd.3.html)。

本地实测表明新建私有文件会出现 com.apple.provenance 标记，尝试移除后仍可读取。因此策略显式允许这一名称（不允许其他属性），而非声称副本一定无 xattr。临时后镜像接受相同检查；普通扩展属性和实际 Darwin ACL 的拒绝均有回归。本片不声称保留所有平台元数据或防止同 UID 进程篡改。

持久性测试包括根级/嵌套文件各 9 个 os._exit 写切点和 2 个 building 导入切点；状态机/错误码/Schema、源目录不变、取消/磁盘失败、归因不足和重复执行见 testing-and-evals 第 19 节。fsync 顺序经过验证，但尚未做断电、网络文件系统或硬件缓存耐久性验收，仅面向本机受管副本。
