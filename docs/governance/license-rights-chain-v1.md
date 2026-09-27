---
doc_type: governance
status: current
version: 2
code_revision: 880c3065482c00d4b0761c739c3ff94f7a7d00cb
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

本次技术审查登记Harnessix Code的许可证、商业授权、贡献声明、商标和第三方通知文件。文件存在且可复核，当前本机许可证报告命中[策略](../../governance/license-policy-v1.json)；但扫描尚未绑定锁定包版本和发行Archive证据，不能据此认定全部历史贡献权利链完整、所有发行物义务已履行或不存在发布阻断问题。当前已复现缺口与完成条件见[0.9.4安全详设](../changes/m09-4-security-and-supply-chain.md#131-已复现的发布阻断缺口)。

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

依赖白名单扫描由[`license_scan.py`](../../scripts/license_scan.py)按`uv.lock`全量执行，报告见[许可证扫描报告](../../governance/license-scan-v1.json)；SBOM由[`sbom_generate.py`](../../scripts/sbom_generate.py)生成版本化库存[`governance/sbom.cyclonedx.json`](../../governance/sbom.cyclonedx.json)。该库存包含73个第三方组件、根应用和全部平台/Extra/开发依赖的锁定关系，属于pre-build清单，不等同于实际安装或发行物内容清单。格式以固定上游CycloneDX 1.5 Schema离线验证，字节漂移检查不得依赖未跟踪的`dist`产物；设计及失败回归见[可复现SBOM详设](../changes/m09-4b-reproducible-sbom.md)。当前74个锁定包（含自有组件和平台条件包）在本机扫描报告命中策略白名单；跨平台许可证元数据、人工覆盖依据及实际发行物义务仍须继续核验，不能仅据零违规报告推断完整权利链。平台条件包（colorama、pywin32、httpx2-jsfetch）在策略的`overrides`登记人工复核结论。

[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)已补充`httpx2`与`httpx2-jsfetch`：二者由Anthropic Python SDK传递引入（Pydantic维护的HTTPX延续项目，BSD-3-Clause），产品Anthropic适配器直接使用`httpx2`，但此前通知文件仅登记HTTPX。内置Eval数据的三个派生Benchmark Archive各自携带完整`LICENSE`与`PROVENANCE.md`，上游许可证与固定Revision可复核。

## 4. 持续门禁

- `make supply-chain`与CI python作业执行许可证扫描、SBOM漂移检查与Secret扫描；任何依赖变更必须同步更新报告，否则CI失败。
- 新增依赖必须同时落入许可证策略白名单并更新[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)（直接依赖与产品直接使用的传递依赖）。
- 工具或规则集升级产生的新结论须经评审后以新版本策略/报告登记，不静默放行。

## 5. 边界

本审查不覆盖：运行时动态下载（产品默认不做）、用户自备MCP Server/Skill/Hook的许可证（由扩展来源审查约束）、以及各国司法辖区对AGPL与商业条款的最终解释。
