---
doc_type: validation-evidence
status: current
version: 4
code_revision: 9dc4647eab94cda362dd68fc33a3127cc30815cc
owners: [core]
modules: [product_config, delivery, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_material_snapshot_differential.py
  - tests/governance/test_git_material_snapshot_differential.py
  - tests/governance/test_git_minimum_commit_probe.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# 真实快照输入与对象写入单变量对照验证

## 1. 结论与边界

新增四格直接Git helper对照已经取得本机及一次固定Windows现场结果：三个步骤成功、一个步骤失败，
原SDK步骤失败，整体Run失败。支持namespace持有后写入路径存在差异，**不确认具体失败调用或历史唯一根因**。
原Windows Run37118277852的两项Git128、SDK失败及Root UNKNOWN保持，不用本机结果替换。
没有生产代码、批准合同、Owner、数据库、模型预算或成功门变更；R1～R6仍开放。

## 2. 需求、设计与源码

[完整设计第13节](../../changes/m09-r4-windows-minimum-commit-probe.md#13-真实快照输入与对象写入的单变量对照)
包含需求、总体架构、流程／时序／数据流、字段、源码位置、伪代码、失败、清理、安全与解释规则。

- [真实对照测试](../../../tests/product_config/test_git_material_snapshot_differential.py)：原SHA256最低Commit、
  原RO普通文件快照、_command、_namespace及_git；无写臂只删除唯一-w，stderr空设备。
- [治理负对照](../../../tests/governance/test_git_material_snapshot_differential.py)：命令、期限、身份准入及原SDK步骤不变。
- [既有手动载体](../../../.github/workflows/windows-git-minimum-commit-probe.yml)：仅预检官方PE／PDB，
  四格分步骤执行，再执行原两个SDK；不上传业务日志、不新增收集器。
- [模块设计](../../modules/product-config.md)：直接helper测试与正式批准／Owner证明的边界。

测试域_GitInvocation不是正式GitMaterialInput；原构造器的purpose_digest校验继续拒绝未认证修改。
临时调用参数不进入worker握手，不具有Nonce、MAC、Proof或批准含义。

## 3. 实际本机验证

| 范围 | 结果 | 可证明内容 |
| --- | --- | --- |
| 初版17项 | 4失败、13通过 | 直接replace正式请求触发原purpose_digest拒绝；失败原件保留 |
| 修正后的四格及关联治理 | 511通过、0失败／错误／跳过 | 实际只读快照、无写效果、写入回读及既有治理合同 |
| 原两个SDK选择器 | 2通过、0失败／错误／跳过 | 本机原业务批准、Owner及独立回读，不外推Windows |
| 末端期限负例原测试 | 2失败 | 效果／发行身份复核跨过真实短预算后原测试仍成功，独立P2复现 |
| 末端期限整改后关联回归 | 513通过、0失败／错误／跳过 | 两个超期负例现在明确拒绝，四格及全部原治理继续通过 |
| 工作流上下文负例 | 1失败 | job env错误引用runner.temp，GitHub HTTP422拒绝且没有创建Run |
| 步骤env整改后关联回归 | 514通过、0失败／错误／跳过 | 结构负例及全部原真实／治理回归通过 |

本机使用Git2.53.0和Python3.12，独立fresh basetemp；上述范围不是全仓、三平台或商用验收。
511项已包含四格和新增治理，不能重复相加。四格均使用相同算法生成的最低合法SHA256 Commit，
不能把它外推为8MiB及全部类型已通过新原生候选。
限定独立审查未发现P0／P1，发现的一个P2末端到期缺口已在测试域修复。
整改后在效果检查前后及最终发行身份复核后复用原预算；新增两个真实Git负例等待至期限之后，
保留原RED，不mock快照、Git或正文。513项包含原511项，不重复累加。
首次dispatch的语法拒绝不是原生Git失败。符号根已按官方上下文合同移入四个步骤env，
job env不再引用runner；514项包含原513项，保留原HTTP422和1失败负例。

## 4. 固定输入、安全与观察

十八既有固定输入仅更新手动workflow一行的LF／CRLF长度及摘要；其余17行、官方PE／PDB、
原两SDK选择器、13接点和20／45／240／300秒合同保持。新增测试身份另外由候选checkout SHA绑定。
每格新增步骤一分钟，回读共享原剩余预算，不延长原SDK五分钟。

四格原生结果只读取GitHub Run／Job／step元数据。预检失败、步骤跳过或缺失不能登记为通过；
任何失败继续保留整体失败，不使用continue-on-error或改变SDK成功判据。
没有读取或发布stdout、stderr、CDB业务日志，没有模型调用或凭据读取。

## 5. 交付、原件与后继

[结构化验证](verification.json)记录实际本机和原生步骤元数据。
完整本机日志／XML、设计前置记录、输入差分、三幅实渲染图、静态结果和Review Packet
归档于私有windows-git-snapshot-differential-20261003-v1目录，0700目录／0600文件。

固定[Run37125374856](https://github.com/carrie1988/Harnessix/actions/runs/37125374856)、attempt1、
候选9dc4647、Job111209499069已终态failure；checkout、锁定依赖和原官方身份仅预检成功。

| 原生步骤 | 实际结论 | 边界 |
| --- | --- | --- |
| hash-direct | success | 直接无写控制步骤成功 |
| hash-held | success | 原namespace持有的无写控制步骤成功 |
| write-direct | success | 不额外持有namespace的写入及回读控制步骤成功 |
| write-held | failure | 持有原namespace的写入控制步骤失败；具体调用阶段未观察 |
| 原两个SDK及低敏侧车 | failure | 仅取得步骤结论；不宣称分别的case退出码或新Git128记录 |

只读取Run／Job／step API元数据，没有读取日志或CDB输出，没有新增有限投影Schema或上传器。
四格间fresh根、初始Commit时间和句柄身份仍不同；步骤失败也可能来自准备或后验断言。
因此不把此结果升级为某个Win32共享调用的唯一根因，不自动放宽生产共享模式或删除原持有。
下一步在相同初始对象库的真实控制下分辨目录／文件持有及实际对象创建、链接／重命名路径，
再以最小生产修复和原保护反例、原SDK回读验收闭环。直接helper通过不替代Supervisor链，Root仍UNKNOWN。
完整Git／Backup v2、真实R3及费用未决、Windows11消费者、独立Beta及最终同候选发布继续独立验收。
