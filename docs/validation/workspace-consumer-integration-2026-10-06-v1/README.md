---
doc_type: validation-evidence
status: current
version: 1
code_revision: b1fe1b629d28001916cef29d5ee3a50462f357ee
owners: [core]
modules: [workspace, delivery, execution, trusted-actions, product-config, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_workspace_record_v3.py
  - tests/delivery/test_parent_closure_runtime.py
  - tests/execution/test_parent_closure_store.py
  - tests/trusted_actions/test_parent_closure_store.py
  - tests/product_config/test_state_backup_parent_closure.py
  - tests/product_config/test_parent_closure_product_sdk.py
  - tests/trusted_actions/test_parent_closure_integration_review.py
supersedes: []
---

# 完整父目录历史：默认 Patch／Rollback 消费者联合接入验收

## 1. 背景、目标与验收范围

独立Snapshot v2端口已能完整保存派生父目录，但旧默认产品仍使用逐项Snapshot v1。
本增量把完整父历史接入默认Patch、独立Rollback、批准指纹、持久当前行及全部事件、完整状态备份与恢复。
不通过删除父观察、提高限额、拆分事务或隐式接受新字段解决容量问题。

实现基线为`b1fe1b6`；基线SHA不是新增代码的发布SHA。完整架构、数据流、时序、类／字段、
接口、伪代码、异常及升级说明见[联合接入详设第13节](../../changes/m09-r4-workspace-parent-closure.md#13-联合接入的实现与源码阅读入口)。
结构化结果见[result.json](result.json)，最终源码／测试／脚本／Schema身份见
[source-inputs.json](source-inputs.json)，摘要目录见[SHA256SUMS](SHA256SUMS)。

## 2. 当前实现与源码入口

| 边界 | 当前实际合同和实现 |
|---|---|
| 快照与完整父历史 | [Snapshot v2](../../../src/harnessix/workspace/snapshot_v2.py)沿用全部原生身份、权限、直接成员前置；唯一原CAS承载Manifest及所有Chunk。 |
| 领域与物理记录 | [Plan2／Record2](../../../src/harnessix/delivery/workspace_v2_contracts.py)、[Stored3](../../../src/harnessix/delivery/workspace_record_v3_contracts.py)；[完整Reader](../../../src/harnessix/delivery/workspace_record_codec.py)还原实际新类型并列出全部历史引用。 |
| 规划与效果 | [Planner2](../../../src/harnessix/delivery/planner_v2.py)复用原镜像规划；[原事务状态机](../../../src/harnessix/delivery/filesystem.py)仍要求精确批准及Lease，恢复不追认prepared外部效果。 |
| 计划与审批 | [Execution3](../../../src/harnessix/execution/versioned_contracts.py)、[Route2](../../../src/harnessix/trusted_actions/versioned_contracts.py)完整绑定新Snapshot及指纹；旧合同字节不扩张。 |
| 默认宿主 | [原装配](../../../src/harnessix/product_config/action_runtime.py)持有唯一Workspace Store并共享CAS；[工具上下文](../../../src/harnessix/product_config/action_composition.py)只对Patch／Rollback启用新代际。 |
| 备份与恢复 | [原只读跨Store校验](../../../src/harnessix/product_config/state_backup_records.py)检查当前及全部历史Plan／Manifest／Chunk与inventory一致；恢复沿用原Owner和同机认证Key。 |

## 3. 验证方法与当前证据

使用本机Darwin arm64、Python3.12.7和实际POSIX文件系统；普通Git副本隔离无关未受管输入，
受管源码、测试、脚本、Schema按有限清单逐件核对。离线Provider只替换模型网络，产品Key、Root Owner、
Session、审批、Router、Lease、写状态机及备份恢复均实际运行。

默认SDK关联范围55项通过，86.594秒；其中新增三项覆盖16个深链文件及401个完整父观察、
批准发布、独立逆向、重启、完整备份恢复、恢复后再开、等待取消及父目录成员改变拒绝。
未知metadata回归65项通过，21.549秒；实际新增代际已支持，未知拒绝用例改为真正未知值，
原拒绝逻辑和原失败证据不删除。

已有263件Schema逐件保持原字节，新六件单独导出；459件生产源码的Mypy通过。
新两幅架构和时序图实际渲染并检查。离线Wheel与459件源文件逐件原字节匹配；
首轮完整范围11837项结算为11682通过／137跳过／18失败，1620.951秒；
全部18失败位于原Windows selected-layout门禁，测试临时根包含`Application Support`空格，
原简单输出路径保护在预期字节／布局负对照之前拒绝。修正测试根为无空格路径，不修改源码门禁／负对照；
最终候选完整治理1413项全部通过，41.561秒，包含原18失败节点。

最终Wheel源码外关联1053项全部通过，137.01秒；含Execution、Route、原Gateway、
新旧事务Reader、完整父历史备份、默认认证SDK及旧Patch／Rollback恢复用例。
所有已导入的Harnessix模块均来自安装目录；源码目录不在导入路径。
原子审批／Owner／MAC／Lease和实际写入未使用替身，只有离线模型替换网络。
安装验证出现一个pytest对已加载asyncio模块的assert重写提示，不是产品失败。
原2分钟SDK门槛未修改；1053项组合套件总时长不是单个SDK场景门槛。

完整首轮冻结发生在独立评审修复前；最终候选另外通过全部关联安装和完整治理，
不把这些范围合成虚构的最终同候选全量零失败。各关联、治理、完整范围有交叠，不能相加。

## 4. 首次失败与整改闭环

- 真实容量首轮11项中2失败：错误地把原5秒读取期限应用到整个同步发布。整改恢复原同步入口语义，
  上游可以传入同一父控制；原局部读取期限未扩大，原子成员写入仍按原效果／记账规则结算。
- 第二轮11项中1失败：测试把发布和独立逆向共用同一已消耗300秒Lease。
  改为两次独立批准分别取得原300秒Lease，不延长TTL、不使用假时钟、不裁剪叶或历史。
- 后继一次运行缺最终JUnit且进程已退出，保留为不完整，不计通过；最终容量结论以完整回归的实际用例结算为准。
- 全树Ruff首次格式检查指出一个SQL字符串换行问题。全量测试冻结期间不改动该输入；
  后继唯一SQL字符串换行整改已验证AST和全部字符串值相等；最终全树Ruff格式与检查均通过，原FAIL保留。
- 已支持metadata的旧未知值断言失败原件保留，后继真正未知代际回归独立记录；不通过删掉拒绝断言取得绿灯。

独立只读评审另发现两项P2：宿主checkpoint被覆盖，以及缺省版本标签的旧历史被新discriminator拒绝。
真实回归首轮49项为15失败／34通过，原件保留；整改保留原宿主与Turn控制、旧Reader输入合同及原SQL正文。
新代际缺标签和显式未知代际仍拒绝；非法深度另有12项实际FAIL，
限定数据解析错误边界后最终64项全部通过，0.836秒，原上游控制异常仍原样传播。源码、流程及回归入口见
[控制与缺省标签设计](../../changes/m09-r4-workspace-parent-closure.md#136-宿主控制组合与旧历史缺省标签)。

## 5. 兼容、限额与不变量

新代际仅在首次新记录的原SQL事务内推进metadata：Workspace2→3、Execution1→2、Audit2→3。
旧记录原字节和旧模型Schema不迁移；只读Reader接受规定代际但不写库。旧程序不应打开含新记录的状态库，
升级整个包前须用原命令完成认证备份，不单独替换Reader。

原256显式资源、32MiB捕获、8MiB单件CAS／镜像、512KiB物理记录、64MiB列累计、
256MiB和2GiB备份上限、公开Patch16文件、Scope/MAC、Root Owner和Lease不变。
原Native18件来源目录、27项选择器、2分钟SDK、13项Hooks和20／45／240／300秒门槛不放宽。
领域128／255叶事务不等于公开Patch新增128／255文件能力。

## 6. 发布结论与未完成范围

**默认Patch／Rollback完整父历史消费者增量通过有界验收；商用1.0仍不验收。**T保持prepared且不向干净A发布的显式来源基础能力保留；
默认认证Git Bridge与A／T／D／独立Commit仍待联合装配。原Process消费者仍使用旧Snapshot与Execution合同。
源码内或脚本模型结果不能外推真实R3编码质量、Windows新候选原生成功、消费者安装部署、独立Beta或R1～R6完成。
本轮真实模型请求为0，原费用未决和预留不改变。
