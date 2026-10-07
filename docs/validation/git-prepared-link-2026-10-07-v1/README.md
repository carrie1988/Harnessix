---
doc_type: validation-evidence
status: reviewing
version: 5
code_revision: 29402f764eae88d50364a37817635fbb77ba907b
owners: [core]
modules: [product_config, delivery, session, artifacts, trusted_actions]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_prepared_link_contracts.py
  - tests/product_config/test_git_prepared_link_ledger.py
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/product_config/test_git_checkpoint_preparation_digest.py
  - tests/agent/test_publication_scheduling.py
  - tests/delivery/test_git_prefix_ledger.py
supersedes: []
---

# 待审批 Git 业务关联组件验证

## 1. 范围与判定

验证[实际详细设计](../../changes/m09-r4-git-prepared-link.md)对应的 prepared 业务事实形成、原 GitDB v2 同事务认证、全部关联只读回读、事务回滚与原身份重试。
不是默认 Git 写工具、实际业务批准、Checkpoint/Commit 效果、完整历史业务恢复、Backup2、真实模型质量或商业发布验收。

当前为 `PASS_COMPONENT_NOT_RELEASE`：同一固定安装源码的本机组件专项通过，不是商业验收。
分候选统计以[结构化结果](result.json)为准；[源输入](source-inputs.json)绑定完整显式输入，
[SHA256SUMS](SHA256SUMS)覆盖本公开包。原始 XML、导入路径及失败原件保存在专用私有交付目录，公开报告记录实际摘要。
元数据 `code_revision` 是源码研究基线；新增模块及当前候选以完整输入和固定 Wheel 字节证明绑定，
不表示新增实现已经包含在该基线提交中。

## 2. 证据分类

- 声明级合同：完整原 Plan2/审批模型、原 CAS 声明材料、严格快照、字节预算、规范 JSON 和回调身份。
- 实际离线原认证 SDK：原成功 Patch/Checkpoint 准备、Review、真实待审批事件、原 Key/Scope 与真实 SQLite 发布/提交/重开回读。
- 故障注入：原 MAC 阳性但业务错误的材料、损坏 Artifact/CAS/尾锚、资源或物理文件替换、取消/期限/事务代际；不能冒充原生 Git 写失败验收。
- 源码外安装：隔离安装实际新 Wheel，全部加载的生产模块来自安装目录；测试宿主与依赖仍使用既有受管环境，不是全新依赖安装证明。

共享观察测试默认仍断言产品不创建 GitDB；新增业务测试显式拥有测试 GitDB 并要求实际文件存在，不能用移动或删除数据库来隐藏持久化结果。

## 3. 原边界保持

原 281 份 Schema、原 GitDB DDL、原六份政策/CI/Native 门禁、原容量与期限保持；只新增 prepared-link/v1 Schema。
类型签名增加新封套，但原规范编码算法及旧解码分派保持。
原新增 NativeBridge 索引仍失败关闭，默认写工具未注册，不默认启动迁移或创建原 GitDB。

## 4. 失败原件与审查

原失败、最终 XML、实际安装导入路径、完整源码/Wheel字节比对、五幅渲染图及专用验证目录 manifest 均保留。
审批阶段误用、遗漏原历史必需参数、共享默认目录断言与显式测试创建的区别，以及可读性快照未同步等问题分别记录，不修改原阈值或追认失败。
详细审查范围见 [Review Packet](review-packet.md)。

完整安装首轮为 336 通过／5 失败；五项均在 SDK 等待审批时失败，不是已进入业务关联后五个断言失败。
关联安装 3784 通过、治理 1413 通过及控制 269 通过属于各自范围，不能相加、覆盖完整失败或证明商业通过。
两个原失败单例在独立诊断中通过，不单独据此归因于验证环境。
同步准备的源码摘要热路径定位及候选整改见[原准备设计](../../changes/m09-r4-git-checkpoint-preparation.md#13-同步准备热路径的候选整改)。
成本机制回归初始一次验证工具故障、一次 1 失败／2 通过及后继 3 通过分别保留；
不提高原 10 秒同步扫描上限或 60 秒准备期限、不缓存源码 SHA、不把插桩失败计作完整真实任务质量结果。

前序原生字段名候选 Wheel 摘要为
`8ffded4c7d0dfd656a201decba70272ac708668c88fee49308c16857cb23c602`。
其源码外控制回归 272 项、治理回归 1413 项均通过，失败、错误及跳过均为零；
生产模块实际从该 Wheel 的隔离安装目录加载，子进程显式沿用安装目录而非源码目录。
该候选完整 prepared 专项为 342 通过／2 失败，关联集合为 3783 通过／1 失败；失败原件保留。
这不是当前生产源码对应的最终安装结果。

后继调度诊断发现一次 19.31 秒事件循环阻塞，微小公开输入因此在实际扫描前后误消耗扫描期限。
[公开保护详细设计](../../changes/m09-4a-product-publication-boundary.md#62-调度等待与同步扫描期限的整改设计)
明确分开调度交接与同步扫描：保留原 10 秒同步扫描检查、完整工作限额、前后取消交接及原外层操作期限。
同步 Git 工作的响应性仍需独立处理，不能由此宣称已经满足延迟要求。

当前固定安装候选 Wheel 摘要为
`fd520997db65ce7ed09217867786f976bcef4bf154ba685e9c87e6da7cc8f7fa`；
545 份包成员（其中 504 份 Python 源码）在主工作区、隔离候选、Wheel 和安装目录逐字节相等。
该候选完整 prepared 专项 377 项、公开保护 358 项、控制 305 项、治理 1413 项、关联 3784 项，
以及后继取消交叉集合 48 项通过，失败、错误及跳过均为零；
每组均核对实际安装导入来源，子进程显式沿用安装环境。
各组存在重叠，不相加为互不重复的测试数。
后继取消集合复用原 33 项并新增 15 项交叉回归，不改变生产源码，不刷新各外层操作期限。
原 Schema 生成、504 份源码类型检查、格式及可读性政策通过；公开包封存与静态结果以结构化报告为准。
本机组件通过不代表完整产品全套测试、依赖全新安装、三平台原生通过、响应性 SLA、真实模型质量或真实用户试用。

## 5. 未关闭的发布工作

完整 ProductLink 生命周期、实际新批准、NativeBridge、A/T2/D、独立 Commit、全业务对象目录、业务 Backup2、新根重新授权、R3 真实质量与费用结算、三平台消费者、独立 Beta 和同候选 R1～R6 仍开放。
当前 prepared 回读依赖原审批仍待决定、当前来源及 Review 有效，不能拿它代替历史备份 Loader。
