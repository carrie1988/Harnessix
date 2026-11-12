---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [workspace, delivery, trusted_actions]
related_adrs:
  - docs/adr/0142-stale-approved-plan-fails-without-effect.md
  - docs/adr/0137-create-with-missing-parent-directories.md
  - docs/adr/0065-platform-capability-ports-and-execution-plan.md
related_tests:
  - tests/workspace/test_plan_binding_ignores_directory_members.py
  - tests/product_config/test_parent_closure_product_sdk.py
  - tests/product_config/test_stale_approved_plan.py
supersedes: []
---

# ADR 0143 计划对目录只绑定对象与权限，不绑定成员

- 状态：评审中
- 日期：2026-11-12
- 修订：Workspace快照“目录成员身份”的绑定（`docs/modules/workspace.md`）

## 背景

操作在审批前会捕获一份Workspace快照，执行前逐项复核，任何一项变化都判定计划过期（`execution_plan_stale`）。
快照里的目录——补丁目标的全部父目录（含工作区根）、命令的工作目录、命令声明的工作区根——此前绑定三样东西：
目录对象与权限、链接数、全部成员的名称与对象身份。

Beta用户在0.10.0b20上的真实复测（vLLM仓库）：新建测试文件的审批窗口开了111秒，期间另一个会话往同一仓库
写了日志文件，批准后计划被判过期，获批的文件没有写入。编辑器的临时文件、测试缓存目录、其他终端的输出都会
触发同样的结果，修改文件和需要审批的命令都受影响。在macOS（APFS）上目录的链接数随成员数变化，任何成员增减
还会改变“待新建文件”的身份。

[ADR 0142](0142-stale-approved-plan-fails-without-effect.md)（0.10.0b21）已经让这种过期不再中断整轮任务：
Agent按当前内容重新发起，用户再批准一次。负责人的决定是两步都做：过期按“未执行”处理之外，还要收窄绑定，
让无关文件的出现根本不使计划过期。

## 决策

1. **计划快照对目录只绑定对象与权限。** POSIX为设备号、inode、模式、属主与属组；Windows为卷序列号、
   File Index与只读属性（沿用原有的稳定身份）。链接数与成员列表不再进入快照；目录观察的大小（成员数）记为0。
2. **缺失的目标绑定“父目录的上述身份＋名称”**；父目录也不存在时，绑定最近的已有目录的上述身份加其下的路径
   （[ADR 0137](0137-create-with-missing-parent-directories.md)的规则不变，只是所用的目录身份随第1条收窄）。
3. **两代快照、所有角色的目录一致适用**：显式作为资源的目录、父目录历史里的目录、工作目录、外部根下的目录。
   没有哪一种操作把“目录里有哪些成员”作为执行的依据：补丁按路径逐段解析，命令在执行时才读取目录。
4. **原生观察本身不变。** 读取工具、安全目录读取等仍然使用含成员摘要的观察身份；观察对象新增一个
   “计划绑定身份”，只有快照的两处编码使用它。

仍然使计划过期的变化：

- 目标文件的内容、模式或对象变化；待新建的路径上出现了文件或目录；待补建的目录被别人先建出来；
- 任何一级父目录被替换为另一个目录（即使同名、内容相同），或其权限、属主变化；
- 路径上出现符号链接或非目录（原有的拒绝规则不变）。

## 取舍

- **安全性。** 成员绑定原本能发现“规划到执行之间目录里多了或少了东西”。对补丁来说，写入的位置由路径上每一段
  目录的对象身份和目标自身的观察决定，兄弟成员不参与解析；大小写不敏感的文件系统上，同名不同大小写的成员会被
  目标自身的观察发现。对命令来说，计划从未绑定命令会读到的全部文件，只绑定根与工作目录的成员列表既不充分
  也不必要。因此收窄不减少对“执行的就是批准的那个操作”的保证。
- **仍然枚举目录。** 观察目录时照旧读取成员列表（读取工具依赖它），快照只是不再使用。因此单个目录成员数
  超过10,000时快照失败的限制仍在；让计划快照跳过枚举需要改动两个平台的观察接口，留作后续。
- **升级时的在途计划。** 升级前捕获、升级后才执行的计划，目录身份的算法不同，会被判为过期并按
  ADR 0142以“未执行”结束，Agent重新发起即可。已经完成的修改的回滚不受影响：回滚按文件镜像另行捕获快照。

## 后果

- 审批窗口打开期间，工作区里出现或消失无关的文件、目录，不再使已批准的修改或命令失效。
- 快照的`identity`摘要与目录的`size`取值随之变化；合同、Schema与事件格式不变。
- `docs/modules/workspace.md`与`docs/modules/delivery.md`里“绑定目录成员”的描述随本ADR更新。
