---
doc_type: validation-evidence
status: current
version: 1
code_revision: b3a2445f5d0093e08f75027d6bf5d7148d905ecb
owners: [core]
modules: [delivery, workspace, product_config, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_workspace_record_reference.py
  - tests/delivery/test_git_private_source.py
  - tests/delivery/test_rollback_binding.py
  - tests/product_config/test_workspace_reference_backup.py
  - tests/governance/test_windows_git_trace2_input_binding.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# 完整Plan引用记录与私有Git来源：实现及验证报告

## 1. 固定来源、目标与结论

本次在`b3a2445`基线之上实施两个基础依赖：完整Plan的版本化私有CAS存储，
以及Git领域Runtime的明确来源解析端口。正式设计分别见
[完整闭包与引用记录](../../changes/m09-r4-workspace-parent-closure.md#10-架构决策与正式实施合同)和
[T／A／D顺序及来源端口](../../changes/m09-r4-git-projection-ordering.md#101-正式架构决策与来源解析实施合同)。
实现来源以本目录有限摘要清单绑定，不将基线SHA描述为新增代码的已发布SHA。

**本层消除合法长路径Plan保存后同代Reader拒绝的缺陷，并证明真实私有A无需发布T即可物化D及Checkpoint。**
完整父闭包新版、产品Bridge／MAC、默认Git装配、联合业务Backup v2、三平台、R3和1.0均未因此完成。
没有模型请求、费用规则调整、用户Workspace变更或旧证据重写。

## 2. 实现范围与源码映射

| 范围 | 实际实现与正式源码 |
|---|---|
| 物理记录 | [StoredRecord合同](../../../src/harnessix/delivery/workspace_record_contracts.py)及[wire v2 Schema](../../../spec/workspace-stored-record-v2.schema.json)：保留原状态及完整领域摘要，引用完整Plan。 |
| 统一Reader | [codec](../../../src/harnessix/delivery/workspace_record_codec.py)完整回读并核对SHA、大小、指纹，再还原原Record；未知、缺失或篡改拒绝。 |
| 新旧数据库 | [Schema准入](../../../src/harnessix/delivery/workspace_store_schema.py)及[Store](../../../src/harnessix/delivery/store.py)：只读1／2不迁移，可写1仅升级元数据；旧原JSON及事件不重写。 |
| 完整备份 | [历史记录核验](../../../src/harnessix/product_config/state_backup_records.py)及[版本准入](../../../src/harnessix/product_config/state_backup_validation.py)：每条历史Plan引用与镜像须在原清单闭合。 |
| 私有Git来源 | [Runtime](../../../src/harnessix/delivery/git.py)及[回链复核](../../../src/harnessix/delivery/git_source.py)：明确端口仍检查原完整绑定、commonDir、注册及双向回链。 |
| 现行固定输入 | 仅更新原18件目录中`git.py`的四个字节身份叶及对应固定摘要；其余17件、原选择器、期限及负对照不变。 |

原领域Workspace Plan／Record、Snapshot v1及全部既有Action公开Schema保持原字节。
原512 KiB记录读限额、8 MiB Blob、32 MiB镜像、原审批、Root、Scope及Lease边界不扩大。
Plan元数据与文件镜像读取端口分离，但共用原完整CAS IO；回滚仍先校验完整记录及原根，再读before正文。

## 3. 真实场景与失败闭环

1. 真实POSIX相对句柄创建200叶、八层共享父链，组件长度分别12及180字节；原Planner生成209资源和2600字节镜像。
2. 完整Plan不截断，SQL采用有界物理壳；每个状态迁移、同一Store及关闭后只读重开均要求完整Record全等。
3. 完整备份长路径历史夹具的领域记录为691216字节、最大物理记录为595字节；包括历史专属的另一合法JSON字节引用。
   两种JSON承载同一完整领域Plan，备份仍须保留并核验全部实际引用，不能只检查最新引用。
4. 真实detached私有A生成和保存原prepared T，原Git／Lease在D物化增删改及二进制文件并持久保存Checkpoint；A保持干净、原Snapshot仍有效。
5. 缺Blob、错字节、大小／指纹／版本、跨Plan、只读写入、耐久确认丢失、旧pretty v1及双Owner陈旧推进均失败关闭。

原长路径复现FAIL保留。集成期间又发现限额内深层JSON的递归异常分类缺口，四项反例先失败；
补齐共享Reader错误分类后通过。原错根回滚测试另揭示Reader混用文件镜像端口；保持原测试不变，
整改读取边界并增加合法根及损坏Plan负对照后，完整相关60项通过。

备份五状态夹具通过原状态函数保存历史，**不表示200叶已经真实发布到Workspace**。
私有A／D验证属于领域组件，不含产品Session／Router／Bridge MAC的联合批准或默认安装链。

## 4. 回归、静态检查与独立复审

回归结果以[result.json](result.json)为唯一结构化计数；嵌套范围不得累加为新的用例总数。
完整测试使用普通干净Git副本，排除非受管个人资料和旧本地产物，不改测试选择器或测试阈值。
完整受管生产代码、测试、脚本及Schema原字节在执行前逐件核对；结果补充后的正式资料另行执行最终文档及治理门禁。

Ruff格式／检查、442件生产源码Mypy、生成Schema、原可读性policy及Secret扫描分别验真。
既有领域v1 Schema未变化，新增物理v2 Schema不覆盖旧文件。
独立静态复审在所列Record／Store／备份／Git端口及测试范围内未发现具体P1／P2；
不把静态复审替代真实运行，也不认定其覆盖未审查的原生IO或后继产品合同。

原工作目录治理运行保留2项失败：可读性报告与当时源码不一致，默认Secret扫描触及旧本地产物的原条目上限。
保持限额与policy不变，在逐件原字节相等的干净副本中复验，不能删除原FAIL或将未完成扫描算零命中。

## 5. 后继与商用验收边界

1. 新Snapshot完整父闭包：保留逐父目录历史，通过原CAS引用承载，原生逐项复核；Route和Planner共享规范派生。
2. Execution／Delivery新代际及批准链联合适配，完整Snapshot全等与指纹绑定保留；不向模型新增CAS配置输入。
3. 产品prepared T／认证Bridge／明确A／D／Checkpoint／独立Commit批准、联合Backup v2及新根恢复。
4. 新候选三平台安装和核心原生验收、R3真实编码质量、独立Beta和同候选R1～R6门禁。

原固定007候选Run37149652886整体failure，Windows Job111280603207终态cancelled；
步骤17元数据仍保留in_progress，不能猜测原生用例数、唯一挂起原因或将其转作新候选通过。
原材料组与前置步骤结果分层保留，见[原观察报告](../git-diff-history-successor-2026-10-04-v1/README.md)。
许可证仍保持原十二件失败，低优先并行处理，不阻挡本层功能实现，也不清除正式发布门禁。

## 6. 验证资料索引

- [result.json](result.json)：实际结果、原失败闭环、有限源身份与验收边界。
- [SHA256SUMS](SHA256SUMS)：公开有限资料的完整摘要。

完整JUnit、原本地日志、源码清单、失败原件、静态复审及Review Packet保存在单独私有交付包；
公开报告不复制数据库、Key、Workspace正文、原CI业务日志或绝对Root。
