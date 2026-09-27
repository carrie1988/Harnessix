---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 1f483ceb267fe2d15ca4d53f794184fd6aa76ecc
owners:
  - core
modules:
  - documentation
  - secrets
related_adrs:
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_license_archive_evidence.py
  - tests/governance/test_secret_scan.py
  - tests/governance/test_supply_chain.py
  - tests/governance/test_cli_console.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 锁定发行物许可证据与跨平台夹具整改验证报告

## 1. 摘要、身份与结论

| 项目 | 冻结事实 |
|---|---|
| 代码 | `1f483ceb267fe2d15ca4d53f794184fd6aa76ecc`；未包含后续证据索引补充提交。 |
| 范围 | SEC-094-B3 Archive级许可来源及B2 Windows测试夹具；[许可详细设计](../../changes/m09-4b-archive-license-evidence.md)、[夹具详细设计](../../changes/m09-4b-secret-fixture-portability.md)。 |
| 环境 | macOS arm64、Python 3.13.8、uv 0.9.1；没有启动Provider验证。 |
| 实际库存 | 74包含根应用、73第三方、777件，声明原包共1,160,324,884字节；203份去重原字节，共1,245,261字节。 |
| 完整回归 | 4055 passed、32 skipped、353.44秒；未跟踪攻击草稿明确排除。 |
| 许可结论 | **12个pywin32 312 Wheel受限正文复核信号，发布门禁返回1**；不是零违规或正式发布PASS。 |
| CI | 旧747fe9b六作业终态：五成功/Windows夹具失败；当前修复在冻结时尚未启动CI，等待真实Windows终态。 |
| 发布判定 | **release_blocked**；0.9.4b、0.9.4、0.9和1.0均未标记完成。 |

## 2. 需求、架构与源码边界

原许可v1只按包名读取本机安装元数据。虚构pytest版本0.0.0与999.0.0得到相同MIT结论，
无来源的名称覆盖也被允许；三个负例均失败。当前[身份合同](../../../scripts/license_contracts.py)、
[采集器](../../../scripts/license_inventory.py)、[决策](../../../scripts/license_decisions.py)和
[离线门禁](../../../scripts/license_scan.py)分责实现每个Archive身份、元数据/通知原字节、SPDX与精确收据。

显式采集仅访问锁定官方TLS URL，不重试、不重定向、不注入凭据；正常CI只读已提交证据，
不访问采集网络。Archive→成员关联可通过完整缓存或重新下载独立重采集，索引哈希本身不能证明提取关系。
Root许可绑定pyproject、锁editable身份及LICENSE摘要，不使用self包名豁免。

Windows旧失败由标准库写入器规范化危险路径及Pytest过长TAR bytes参数ID引起。
[修复测试](../../../tests/governance/test_secret_scan.py)保留全部场景，补短ID与本地/中央记录原名称自检；
没有Windows跳过或扫描规则放宽。Secret扫描仍默认拒绝TAR链接；许可证采集仅可跳过非目标无载荷链接，绝不跟随目标。

## 3. 测试方法、结果与未完成边界

| 验证 | 结果 | 范围 |
|---|---|---|
| v1许可负例 | 3 failed | 复现错误来源/版本继承，不使用真实凭据。 |
| Windows夹具修复相关组 | 113 passed，10.69秒 | 91项Secret专项与治理/CLI交叠组；专项最长节点ID119字符，不代表真实Windows已通过。 |
| 最终专项组 | 167 passed，11.83秒 | 54许可、91 Secret及既有CLI/供应链用例；与完整回归重叠，不相加统计。 |
| 初轮完整回归 | 1 failed、4048 passed、32 skipped，350.56秒 | 文档初稿缺语义章节，独立完整重跑见下一行；旧失败保留。 |
| 最终已跟踪完整回归 | 4055 passed、32 skipped，353.44秒 | 命令明确排除未跟踪攻击草稿，不能宣称原始工作树make check通过；证据冻结参数随后专项复验。 |
| 证据冻结补充回归 | 229 passed，19.24秒 | 含新增原字节Manifest参数和版本/许可阻断检查；与完整回归及专项组重叠，不相加统计。 |
| 静态门禁 | Ruff764个Python文件、Mypy328源码、可读性/合同/Task Pack/SBOM通过 | 不绕过当前许可返回1；新增packaging开发边后SBOM同步。 |
| 文档门禁及渲染 | 冻结前287文档、7673链接、703 Mermaid、26源码包；变化图实际渲染通过 | 图渲染使用现有Chrome，证据冻结后门禁为288文档、7691链接，703图数量不变。 |
| 完整缓存重采集 | 777件完成，索引与代码提交逐字节相同 | 不安装或执行下载对象，203份原字节摘要逐项核对。 |
| 真实干净源码打包 | Wheel/sdist两件Secret检查通过，2226输入完整覆盖 | 准确代码git archive构建；不是商用安装/Beta验收，也不证明当前HEAD产物逐字节相同。 |

完整回归与产物构建证明候选控制行为，不证明所有文件级许可或法律权利已审查。
Provider请求为0，未消费百炼预算；公共发行文件采集不属于模型调用。

## 4. 原字节、持久化、恢复与实际发行物

[索引](../../../governance/license-evidence-v2/index.json)记录777件原包身份；
[策略](../../../governance/license-policy-v2.json)仅允许明确SPDX原子及精确旧声明收据，
[报告](../../../governance/license-scan-v2.json)绑定锁/项目/规范策略/索引及根LICENSE摘要。
每个Blob以SHA寻址；失败批次只留下可复核缓存/Blob，不替换旧完整索引。取消、缺件、预算超限和输入组变更失败关闭。
Git属性保持CRLF原字节，不为格式清理改写第三方证据；禁止Blob文本diff/merge不是Secret豁免。

| 产物 | 大小 | 成员数 | SHA-256 |
|---|---:|---:|---|
| Wheel | 925658 | 371 | `59611155f684d89605b364198bcdb6d37fc17d490b1dcd234afd7a162cb88944` |
| sdist | 6838770 | 2204 | `edfb345c797a01882dc57d5f1bf599478683e4ca86379cfa8e043bdb83871f23` |

两件solutions成员均为0。原包不提交，源码不包括下载缓存、未跟踪攻击草稿或用户数据库。
构建后端尚未完全锁定，不声明跨环境可重复构建；上述哈希只归属准确代码提交。

## 5. Manifest、Review Packet与复验

| 文件 | 职责 |
|---|---|
| [verification.json](verification.json) | 原负例/文档失败、独立回归、交叠口径、静态与许可真实失败。 |
| [license-facts.json](license-facts.json) | 输入摘要、777件/203 Blob记录、独立重采集、MCP Windows来源与实际产物。 |
| [ci-observation.json](ci-observation.json) | 747fe9b准确Run/Attempt六终态、Windows原因与当前未开始状态。 |
| [review-packet.json](review-packet.json) | release_blocked及所有独立开放门禁，不追认整体完成。 |
| [bundle-manifest.json](bundle-manifest.json) | 五文件原字节及准确代码输入哈希，自排除。 |

```bash
uv sync --locked --all-extras --dev
uv run python scripts/license_inventory.py
uv run python scripts/license_scan.py --check
uv run pytest tests/governance/test_license_archive_evidence.py tests/governance/test_secret_scan.py
uv run pytest --ignore=tests/security -q -o addopts=''
```

没有原包缓存时采集返回2；需要显式fetch才能联网补齐。当前许可--check必须返回1，不能将其视为工具崩溃或改为continue-on-error。
新CI/新提交的结果须新增证据，不覆盖[前序Secret冻结包](../secret-scan-2026-09-27-v1/README.md)或旧Windows失败。

## 6. 风险与剩余发布门禁

- pywin32原字节正文涉及附属LGPL，12件仍阻断；替换或精确例外及义务评审未完成，不扩大白名单。
- MCP 2.2.0实际源码调用win32api/win32con/win32job处理Windows句柄与Job Object，不能仅删除锁依赖并声称等价。
- 当前Windows夹具候选终态待核验；旧五成功不能用作修复Windows通过。
- Docker/构建后端/文档工具不可变输入及自有贡献权利链仍独立开放。
- 0.9.4a剩余公开边界、编号攻击套件、远端MCP、0.9.5安装/Beta、0.9.6真实Provider仍开放。
- 固定Secret规则零命中、许可声明白名单和本地测试通过均不是1.0商用完成证明。
