---
doc_type: validation-evidence
status: current
version: 1
code_revision: 9eff41bef88f995d7c856c6af543cc627e9cf128
owners: [core]
modules: [workspace, delivery, execution, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/workspace/test_snapshot_parent_closure.py
  - tests/workspace/test_parent_closure_contracts.py
  - tests/workspace/test_parent_closure_reader.py
  - tests/workspace/test_native_observation_control.py
  - tests/workspace/test_snapshot.py
  - tests/workspace/test_snapshot_capacity.py
  - tests/delivery/test_git_projection_capacity.py
  - tests/delivery/test_workspace_record_reference.py
  - tests/product_config/test_workspace_reference_backup.py
supersedes: []
---

# Snapshot v2 完整父目录历史：实现及验证报告

## 1. 来源、目标与验收边界

本增量基于`9eff41b`实现显式Snapshot v2宿主端口，源码／测试／Schema身份由有限清单绑定；
基线SHA不是新增实现的发布SHA。正式总体与详细设计见
[父闭包详设第11节](../../changes/m09-r4-workspace-parent-closure.md#11-snapshot-v2完整父闭包的实施合同)，
模块阅读与调用示例见[Workspace增量](../../modules/workspace.md#snapshot-v2-完整父历史增量)。

**独立端口已实现完整父历史，不代表默认产品消费者已经切换。**
旧默认Planner的128分散叶失败保留；共同Route／Planner、Execution／Delivery批准新代际、
全部事件和业务备份消费者、prepared T／Bridge MAC／A／D／独立Commit仍分别待联合验收。
本机结果不能推导Windows新候选原生、R3、Beta或商用1.0。

## 2. 架构与源码定位

| 实现责任 | 正式源码及合同 |
|---|---|
| 新Snapshot与旧不变量 | [`snapshot_contracts.py`](../../../src/harnessix/workspace/snapshot_contracts.py)和[`snapshot_fields.py`](../../../src/harnessix/workspace/snapshot_fields.py)；原v1字段／摘要域不变，新v2另验精确父数量、目标及根身份。 |
| 完整字典与版本合同 | [`parent_closure_paths.py`](../../../src/harnessix/workspace/parent_closure_paths.py)及[`parent_closure_contracts.py`](../../../src/harnessix/workspace/parent_closure_contracts.py)；派生全部严格祖先及使用根，根优先、共享组件、平台去重、连续完整块。 |
| 原生事实与预算 | [`snapshot_capture.py`](../../../src/harnessix/workspace/snapshot_capture.py)及[`native_observation_io.py`](../../../src/harnessix/workspace/native_observation_io.py)；原POSIX／Windows端口，叶与目录共用原32 MiB，沿用父checkpoint。 |
| 完整编码及Reader | [`parent_closure_codec.py`](../../../src/harnessix/workspace/parent_closure_codec.py)及[`parent_closure_wire.py`](../../../src/harnessix/workspace/parent_closure_wire.py)；原CAS、规范JSON、全量解引用和流式全集摘要。 |
| 显式捕获与只读再验证 | [`snapshot_v2.py`](../../../src/harnessix/workspace/snapshot_v2.py)；耐久回读后才返回，验证没有写入、迁移或补签端口。 |
| 独立导出 | [Snapshot v2](../../../spec/workspace-snapshot-v2.schema.json)、[Manifest v1](../../../spec/workspace-parent-closure-v1.schema.json)、[Chunk v1](../../../spec/workspace-parent-observations-v1.schema.json)；旧260件Schema逐件核对。 |

路径、根、外部授权及完整原生身份／权限／直接成员观察不删减；SHA引用不构成来源MAC或业务批准。
外部write权限不能替代父read授权。workspace不新增delivery／session依赖、第二CAS或中间件。

## 3. 真实场景与失败语义

- 真实127／128／255分散叶，分别保留全部使用根和每个单层父目录；关闭Store后由原只读CAS完整重开。
- 255叶共享40层真实父链，完整41父观察；输入逆序、显式／隐式cwd同一字节及revision。
- 原v1 128叶加全部父项仍`workspace_snapshot_limit`；不改旧用例、限额或Planner。
- 父对象、POSIX权限、直接成员及叶正文变化、换物理根：只读再验证`execution_plan_stale`，CAS不变。
- 四个原8 MiB文件加真实目录枚举超过原32 MiB，捕获拒绝且没有CAS提交；不按父块重置预算。
- 构造绕过、未知／非规范／坏块、缺失／额外／重复／逃逸／错序字典、错根／目标／外部绑定、全集摘要或共享cwd不一致：完整Reader固定拒绝。
- 重分块使用真实已捕获历史；验证读取每个块并返回全部原观察，不返回部分前缀。
- 上游取消／超时保持原异常实例；原生FD和目录迭代器关闭。写入确认丢失可留下无业务引用证据，但没有成功Snapshot或事务行。

检查是协作式边界，不能抢占已进入的系统调用；原绝对根绑定段不改。
Windows逻辑路径合同验证与Windows原生执行明确区分。

## 4. 验证与结构化证据

计数和固定输入以本目录[result.json](result.json)及[SHA256SUMS](SHA256SUMS)为准；开发中间结果与最终候选分开，嵌套范围不累加。
完整回归只在普通干净Git副本运行，原生产、测试、脚本及Schema逐件核对来源；不扫描或测试非受管个人目录。
Ruff、Mypy、原可读性策略、独立Schema、Secret和文档／图示门禁分别记录，不能以其中一项代替业务验收。
实际单一Wheel包含451件生产源码并逐字节匹配；源码外35件导入均来自安装目录，
真实128叶／129父项可关闭后只读重开验证。Python 3.12.7及POSIX实跑不冒充三平台安装通过。

初始新API不存在的收集失败、开发期间文档链接未生成的失败与后继结果均保留。
独立Reader复审发现workspace／cache根历史篡改成missing后仍被接受，两项实际FAIL保留；
整改读取端独立根类型验证后，原77项Reader及总196项新专项全部通过；不改旧FAIL、转xfail或提高阈值。
全量首轮11550项实际11412通过、137平台跳过、1失败、0错误，894.029秒；
唯一失败为源码新增后生成的readability最终报告未同步。保留首轮FAIL及原报告，
以原生成器重新计算报告，变更仅为统计及旧符号位置；原policy、阈值、依赖边和公共API不改。
后继完整治理复验1413项全部通过、零跳过／失败／错误，37.155秒，单独登记，不将其虚写成第二次全量零失败；业务源码、测试、脚本和Schema从全量开始始终不变。
原Windows选择器、材料输入、期限与负对照不减。

## 5. 发布风险及后继任务

1. 联合新代际：共同正式目标派生、Planner／Route、Execution／Delivery／批准与全部历史Reader／业务Backup同时切换。
2. 产品顺序：prepared T保持实际状态，不写入干净A；认证Bridge、受管D／Checkpoint与独立Commit审批闭合。
3. 同候选Windows原生、三平台源码外安装与业务恢复再验证；旧CI和失败不能充当新候选通过。
4. R3真实质量及两笔未决费用、消费者平台、独立Beta、R1～R6与商业权利仍开放。没有模型调用、预算规则修改或自动化。
5. 上一候选CI仅观察Run／Job／步骤元数据；两Linux完整pytest步骤成功但原许可检查失败，本增量不删除其12件pywin32 Wheel正文通知复核项。

资料修订不授予默认产品能力，不宣称商业发布日期或完成比例。
