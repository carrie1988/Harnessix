---
doc_type: validation-evidence
status: current
version: 1
code_revision: 007d2bd7c7f769616ec94641283274990b2acd66
owners: [core]
modules: [governance, documentation]
related_adrs:
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/governance/test_license_archive_evidence.py
  - tests/governance/test_supply_chain.py
supersedes: []
---

# 十二件 Windows 发行物许可通知复核边界验证报告

## 1. 目标、固定输入与结论

本报告核验现有十二件失败能否仅通过补充库存、旧声明收据或去重显示解除，不修改依赖、
策略、收据或扫描实现，不重复采集未变化的全部发行物。
固定源码为 `007d2bd7c7f769616ec94641283274990b2acd66`，复用
[原详细设计](../../changes/m09-4b-archive-license-evidence.md)及既有库存。

**十二件均为 `pywin32 312` 的 Windows Wheel，不是十二个软件包。**
它们覆盖 CPython 3.12～3.15 与 win32、win_amd64、win_arm64 的组合。
每件十五条通知和八个 License-File 声明在现有索引中完整匹配；
受限正文信号仍使原决定为 `violation / license_notice_review_required`。
未发现可机械补齐以消除该决定的库存字段或 Blob 缺口。

原声明 `PSF` 不能覆盖附属通知。即使为旧声明补充准确表达式收据，现有扫描实现仍会以
受限正文信号覆盖该决定。去重显示也不能消除真实成员信号。
因此，**仅补证不构成当前门禁通过；十二件失败继续保留。**

本报告不是法律许可判断、全部分发义务验收或正式商业授权结论。
这些治理工作低优先并行处理，不作为功能研发和内部验证的串行前置。

## 2. 原接口、调用链与字段解释

| 控制点 | 实际职责与结论 |
|---|---|
| [`LockedArchive`](../../../scripts/license_contracts.py) | 名称、版本、Registry、类型、URL、SHA 和字节数绑定一件不可替换发行物；不能以其他平台审批替代。 |
| [`EvidenceBlobs.read`](../../../scripts/license_contracts.py) | 使用原普通文件、摘要、大小、去重及时间／总量预算读取原证据。 |
| [`metadata_facts`](../../../scripts/license_decisions.py) | 核验元数据身份及 License-File 声明，旧 `PSF` 保留为待解释字段，不猜测 SPDX。 |
| [`reviewed_expression`](../../../scripts/license_decisions.py) | 收据精确绑定该件发行物、元数据及全部通知摘要；不能按包名继承。 |
| [`notice_signals`](../../../scripts/license_decisions.py) | 产生受限许可正文复核信号；不自动推导许可后缀或全部适用文件。 |
| [`_archive_decision`](../../../scripts/license_scan.py) | 即使元数据表达式获准，只要存在正文信号，最终决定仍覆盖为通知复核失败。 |

实际验证直接调用原 `_archive_decision` 和原 `EvidenceBlobs`，未复制决策实现或使用替代 Blob 适配器。
十二件返回的完整 entry 与原冻结报告逐字段相等，既有资源限制及文件读取防护同时生效。
未调用全批次报告生成器或正常 CLI；不能据此声称重新完成全部 777 件门禁。

## 3. 验证范围与结构化结果

| 项目 | 本次实际结果 |
|---|---|
| 原冻结库存 | 777 件发行物、74 个包、12 件违规，原报告不变 |
| 有界选择 | 精确十二件 SHA，未对其余发行物执行决策 |
| 原实现复核 | 十二件完整返回记录全部与冻结报告相等 |
| 原读取器 | 九个唯一 Blob，合计 56510 字节，包含元数据与通知 |
| 两个信号成员 | `adodbapi/license.txt` 与 `.dist-info/licenses/adodbapi/license.txt`，不同成员引用相同正文 SHA |
| 活跃输入身份 | 原策略、索引、报告、相关源码及实际锁／项目摘要核验一致 |
| 网络、安装、模型请求 | 均为零 |
| 原 Archive 独立重提取 | 本次未执行；不覆盖既有采集报告，也不新增成员来源证明 |
| 策略／依赖／收据变更 | 无 |
| 完整 pytest／全批次 CLI | 本次未执行；源码关联测试不冒充本次测试成绩 |

结构化 [result.json](result.json) 保存精确十二件身份、原失败、成员信号及输入摘要；
[verification.json](verification.json) 保存原函数、原读取器和逐件完整记录一致结果；
[SHA256SUMS](SHA256SUMS) 提供两份结构化资料摘要。
原 notice 正文、私有路径和临时分析输出不复制进公开包。

## 4. 后继决策、安全与发布边界

如需处置，必须先明确实际附属组件的授权、适用文件及分发范围，再作正式依赖或治理决策。
补充 Archive 成员来源证明可以提高证据完整性，但不会自动取消现行通知信号。
不得伪造旧声明收据、忽略某个成员、删除 Windows 平台或降低拒绝策略制造零违规结果。

当前 R2 继续开放，许可证扫描非零退出保留。完整 Git 产品交付、真实编码质量、消费者平台、
独立 Beta 及同候选 R1～R6 仍分别按原退出条件验收，本报告不关闭商用发布门禁。
