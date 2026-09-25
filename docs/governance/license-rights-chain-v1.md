---
doc_type: governance
status: current
version: 1
code_revision: 7a39f28bfb0d82e75a9e7a8b677c642d7733f845
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

本次审查覆盖Harnessix Code的自有许可证、商业授权、贡献权利链、商标与第三方组件权利链。结论：权利链完整、相互一致、可由仓库内版本化文件复核；第三方依赖全部命中[许可证策略](../../governance/license-policy-v1.json)白名单，无GPL/LGPL/SSPL/专有许可证组件，无阻断发布的问题。本审查只覆盖版本化文件与锁定依赖，不构成法律意见。

## 2. 自有许可证与商业授权

| 治理文件 | 复核结论 |
|---|---|
| [LICENSE](../../LICENSE) | 全文为`AGPL-3.0-only`，与[ADR 0064](../adr/0064-agpl-and-commercial-dual-licensing.md)的切换边界一致。 |
| [COMMERCIAL_LICENSE.md](../../COMMERCIAL_LICENSE.md) | 独立商业授权通道仅授予闭源嵌入/修改/不按AGPL提供源码的主体；不与AGPL文本冲突。 |
| [COPYRIGHT.md](../../COPYRIGHT.md) | 版权所有者唯一且与商业授权主体一致，具备双重授权所需权利基础。 |
| [TRADEMARKS.md](../../TRADEMARKS.md) | 明确代码许可证不授予Harnessix名称与Logo权利，与两许可证正文不矛盾。 |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | 贡献条款保证外部贡献可被纳入AGPL与商业双轨授权，权利链闭合。 |
| 历史MIT版本 | ADR 0064边界之前的发布继续适用MIT，边界之后为AGPL；仓库未改写历史授权。 |

## 3. 第三方组件权利链

依赖白名单扫描由[`license_scan.py`](../../scripts/license_scan.py)按`uv.lock`全量执行，报告见[许可证扫描报告](../../governance/license-scan-v1.json)；SBOM由[`sbom_generate.py`](../../scripts/sbom_generate.py)生成CycloneDX产物[`dist/sbom.cyclonedx.json`](../../dist/sbom.cyclonedx.json)。当前74个锁定包（含平台条件包）全部命中白名单：MIT/BSD/Apache-2.0/PSF-2.0/MPL-2.0。MPL-2.0为文件级弱Copyleft，本项目未修改其源文件，满足其义务。平台条件包（colorama、pywin32、httpx2-jsfetch）无法从本机安装元数据读取许可证，已在策略的`overrides`中登记人工复核结论与依据。

[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)已补充`httpx2`与`httpx2-jsfetch`：二者由Anthropic Python SDK传递引入（Pydantic维护的HTTPX延续项目，BSD-3-Clause），产品Anthropic适配器直接使用`httpx2`，但此前通知文件仅登记HTTPX。内置Eval数据的三个派生Benchmark Archive各自携带完整`LICENSE`与`PROVENANCE.md`，上游许可证与固定Revision可复核。

## 4. 持续门禁

- `make supply-chain`与CI python作业执行许可证扫描、SBOM漂移检查与Secret扫描；任何依赖变更必须同步更新报告，否则CI失败。
- 新增依赖必须同时落入许可证策略白名单并更新[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)（直接依赖与产品直接使用的传递依赖）。
- 工具或规则集升级产生的新结论须经评审后以新版本策略/报告登记，不静默放行。

## 5. 边界

本审查不覆盖：运行时动态下载（产品默认不做）、用户自备MCP Server/Skill/Hook的许可证（由扩展来源审查约束）、以及各国司法辖区对AGPL与商业条款的最终解释。
