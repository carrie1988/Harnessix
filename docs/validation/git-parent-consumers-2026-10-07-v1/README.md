---
doc_type: validation-evidence
status: current
version: 1
code_revision: d7e8668af32866c9e7fc8a31400e64ac98532539
owners: [core]
modules: [workspace, delivery, product_config, trusted_actions]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_parent_closure.py
  - tests/product_config/test_git_parent_consumers.py
  - tests/product_config/test_git_parent_observation.py
  - tests/product_config/test_git_parent_consumer_review.py
supersedes: []
---

# 完整父历史Git消费者验收

## 1. 背景、目标与范围

默认Patch／Rollback已经生成完整父历史，但旧Git来源、基准和领域验证器未使用新代际。
本增量贯通实际Source2／Baseline2及原Git领域Runtime，不裁剪父观察、不拆分事务、不扩大资源或期限。
完整总体／详细设计、流程图、时序图、字段、接口、伪代码及恢复边界见
[Git父历史消费者设计](../../changes/m09-r4-git-parent-consumers.md)。

本报告的`code_revision`是开发基线，不是新增实现发布SHA。最终代码输入绑定
[source-inputs.json](source-inputs.json)；结果、独立评审和摘要分别见
[result.json](result.json)、[Review Packet](review-packet.json)、[SHA256SUMS](SHA256SUMS)。
首轮输入与最终结构整改后的输入分别记录，不将不同候选成绩合并为全量发布门禁。

## 2. 当前实现、架构与源码

| 能力 | 源码与实际边界 |
|---|---|
| 新来源与基准 | [独立新合同](../../../src/harnessix/product_config/git_parent_contracts.py)继承原路径、容量、成员及完整摘要校验；实际新Snapshot字段不经过旧序列化器截断。 |
| 来源链与当前观察 | [来源采集](../../../src/harnessix/product_config/git_delivery_source.py)全量归属预检先于CAS，按持久成功顺序保留首before／末after；净零路径仍参与当前完整观察。 |
| 固定Git基准 | [基准采集](../../../src/harnessix/product_config/git_baseline.py)保持原60秒总预算、固定Query、HEAD／Index／Blob和前后完整观察；新历史使用同一checkpoint。 |
| 历史Reader控制 | [归属Reader](../../../src/harnessix/product_config/workspace_patch_source.py)、[Router](../../../src/harnessix/trusted_actions/router.py)、[Audit](../../../src/harnessix/trusted_actions/store.py)、[Planner](../../../src/harnessix/delivery/trusted_action.py)、[Store](../../../src/harnessix/delivery/store.py)显式传递单次控制，保留原constructor控制及错误对象。 |
| Git领域 | [代际分派](../../../src/harnessix/delivery/git_workspace_snapshot.py)使用原CAS完整历史；[原Runtime](../../../src/harnessix/delivery/git.py)在新Worktree记录之前复核。helper原字节进入原实现摘要。 |

新Source2采集由受信宿主显式提供原唯一CAS端口，只追加不可变材料，不新开写SQL连接、
不写用户Workspace／Index／Ref，不因为端口参数取得身份或批准。没有端口明确拒绝，新记录不降级。
全部269件旧Schema原字节保持，两件新Schema单独导出。
Native18仅`git.py`的bytes／sha256／crlf_bytes／crlf_sha256四个叶更新，其余17件输入原字节保持。

## 3. 验证方法与结果

本机Darwin arm64、Python3.12.7、真实POSIX文件系统与隔离Git配置。
普通Git副本只承载正式已跟踪输入及明确列出的新增文件；无关目录不进入复制、测试、扫描或提交。
所有pytest basetemp使用无空格的短路径；测试环境保留原Native权限要求，证据目录单独收紧权限。
模型只使用离线ScriptedProvider，未调用百炼、读取钥匙串或修改原费用账本。

### 3.1 默认认证SDK

16个深层叶文件／401父观察，分别在SHA-1和SHA-256仓库验证默认审批Patch、Source2、Baseline2、
原MAC Thread重开和同一来源。User文件、Index、HEAD和Route／事务SQL不因来源采集改变；
CAS追加必须显式，原只读Store继续拒绝写入。
缺端口、未授权集合、缺／错历史、父成员／mode／identity、叶漂移、净零观察、
Native正文控制、最终基准取消／期限及历史首块控制均有独立负例。

### 3.2 真实A／T／D与独立Commit

真实私有detached A中准备完整Plan2／Record2，在原D物化并创建Checkpoint。
覆盖1／128／255分散叶、32层父链及SHA-1／SHA-256；A／U保持clean，T始终prepared、sequence／cursor为0。
旧事务指纹不能批准新Commit，精确新Commit指纹才允许原本地分支提交。
原Root／HEAD／commonDir／注册／backlink／Lease与UNKNOWN恢复边界不变；没有向A发布T。

### 3.3 候选、静态、安装与回归

最终治理1413项通过；Ruff检查／格式、Mypy461模块、原readability policy、Schema及文档检查通过。
唯一Wheel含461件逐字节匹配源码；460个导入模块均来自安装目录，源码目录不在sys.path。
最终同一Wheel源码外关联590项全部通过，780.62秒；原255叶发布／独立回滚／全部事件重开仍在此套件中，
另独立复验1项通过，387.06秒，不与590重复累加。原2分钟SDK及各原Lease不变，套件总时长不是SDK门槛。
只有pytest已加载asyncio模块的assert重写提示，没有产品告警或失败。
首轮广泛回归输入不同，3795通过／27跳过后主动停止旧候选；首个安装回归378通过后为定位长用例停止，
全部中止原件保留，不作为整套成功。最终输入的590项关联与1413项治理各自结算，不替代最终整候选全量验收。

## 4. 失败历史、整改与独立评审

首轮新测试缺新合同模块而收集失败；旧Baseline验证符号移动使原两件取消／期限回归失败，
恢复旧验证分支后110项通过。新增per-call控制后，两件旧故障替身未接受新可选参数，
只对齐替身签名而保留原拒绝断言。
独立评审5项红例证明历史CAS未共享单次控制；原Reader逐层传递后48项评审回归通过，
并核对异常identity、混合代际、净零、完整摘要及60秒预算。早期44件setup错误来自不存在的basetemp父目录，
测试主体未执行，不认定功能失败或成功。
首轮治理1412通过／1失败，暴露Router热点增长与Audit.load复杂度21；
只分离单次Reader控制入口，未改policy或阈值，最终1413项通过。
独立Git领域原8件红例、测试错误导入及额外广泛回归中止记录全部保留，未声称被中止套件通过。

## 5. 风险、发布结论与后续必需项

本增量不关闭默认认证GitBridge、完整业务Backup2、Windows消费者安装闭环、R3真实编码质量、Beta或R1—R6。
来源返回值和CAS耐久材料不是已持久业务交付记录，不能追认Git成功或签发批准。

上轮`d7e8668`的[CI 37446435573](https://github.com/carrie1988/Harnessix/actions/runs/37446435573)
整体FAIL：Linux3.12失败于pytest、3.13失败于license_scan，Windows trusted-execution取消；
macOS、文档、Container及三组Windows Native Git任务成功。
仅使用Run／Job／step元数据，Linux pytest失败根因仍未确证，未读取CI业务logs或原生诊断正文。
本机成功不替代这些平台结果或同Revision发布门禁。

下一必需链为认证Source／Baseline至原A／T／D的正式Bridge，完整MAC／Owner／Scope、Diff、独立批准和Lease，
随后业务Git状态备份闭合、Windows原生消费者、既定真实Task Pack及有限Beta。
原70元周期和全部未决预留保持；本增量没有新增模型费用或释放预留。
