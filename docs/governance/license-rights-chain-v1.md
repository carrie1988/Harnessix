---
doc_type: governance
status: current
version: 3
code_revision: 747fe9b6a40bb6596c23772fab686b5df0b1db74
owners:
  - core
modules:
  - documentation
related_adrs:
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
related_tests:
  - tests/governance/test_supply_chain.py
supersedes: []
---

# 许可证权利链审查 v1（0.9.4b）

## 1. 审查范围与结论

本技术审查登记自有许可、商业授权、贡献声明、商标及第三方原字节证据。当前[策略v2](../../governance/license-policy-v2.json)
与[报告v2](../../governance/license-scan-v2.json)绑定全部777个锁定Archive，pywin32 12件包含附属LGPL正文复核信号，
保持真实发布阻断。扫描器能完整读取证据不代表不存在许可冲突，也不代表全部贡献权利或分发义务已核实。
元数据与原通知字段、收据和失败语义见[详细设计](../changes/m09-4b-archive-license-evidence.md)。

## 2. 自有许可证与商业授权

| 治理文件 | 复核结论 |
|---|---|
| [LICENSE](../../LICENSE) | 全文为`AGPL-3.0-only`，与[ADR 0064](../adr/0064-agpl-and-commercial-dual-licensing.md)的切换边界一致。 |
| [COMMERCIAL_LICENSE.md](../../COMMERCIAL_LICENSE.md) | 独立商业授权通道仅授予闭源嵌入/修改/不按AGPL提供源码的主体；不与AGPL文本冲突。 |
| [COPYRIGHT.md](../../COPYRIGHT.md) | 文本声明的版权所有者与商业授权主体一致；独立历史贡献权属证据仍需核验，文本存在不是完整双重授权证明。 |
| [TRADEMARKS.md](../../TRADEMARKS.md) | 明确代码许可证不授予Harnessix名称与Logo权利，与两许可证正文不矛盾。 |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | 存在外部贡献授权条款；实际每项贡献是否获得有效授权仍需逐项来源与同意记录，不能直接认定权利链闭合。 |
| 历史MIT版本 | ADR 0064边界之前的发布继续适用MIT，边界之后为AGPL；仓库未改写历史授权。 |

## 3. 第三方组件权利链

[`license_scan.py`](../../scripts/license_scan.py)离线核验精确版本、Registry、每个Wheel/sdist身份与元数据/通知原字节，
不读本机安装元数据。[原字节库存](../../governance/license-evidence-v2/index.json)覆盖73个第三方组件的777个发行件，
包含全部平台/Extra/开发锁范围。colorama两件旧声明各自有精确Archive收据；不再以包名覆盖平台条件包。
pywin32元数据的`PSF`与实际adodbapi附属LGPL正文不能被等同为纯PSF结论，12件均保持失败关闭。

[SBOM](../../governance/sbom.cyclonedx.json)继续由[生成器](../../scripts/sbom_generate.py)形成pre-build全集，
本次仅显式登记既有packaging 26.3开发依赖边并同步锁/库存摘要，没有升级第三方版本。
[历史v1报告](../../governance/license-scan-v1.json)保留供旧提交复核，不再是当前CLI或发布输入。
Archive元数据声明与采集目录不能证明所有文件级义务或商业权利已经完成。

[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)已补充`httpx2`与`httpx2-jsfetch`：二者由Anthropic Python SDK传递引入（Pydantic维护的HTTPX延续项目，BSD-3-Clause），产品Anthropic适配器直接使用`httpx2`，但此前通知文件仅登记HTTPX。内置Eval数据的三个派生Benchmark Archive各自携带完整`LICENSE`与`PROVENANCE.md`，上游许可证与固定Revision可复核。

## 4. 持续门禁

- `make supply-chain`与CI python作业执行许可证扫描、SBOM漂移检查与Secret扫描；任何依赖变更必须同步更新报告，否则CI失败。
- 新增依赖必须同时落入许可证策略白名单并更新[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)及Archive证据集合（直接依赖与产品直接使用的传递依赖）。
- 工具或规则集升级产生的新结论须经评审后以新版本策略/报告登记，不静默放行。

## 5. 边界

本审查不覆盖：运行时动态下载（产品默认不做）、用户自备MCP Server/Skill/Hook的许可证（由扩展来源审查约束）、以及各国司法辖区对AGPL与商业条款的最终解释。
