---
doc_type: validation-evidence
status: current
version: 1
code_revision: 21b5eb1d57055f32ba2178c165b3ad46060ee7c7
owners:
  - core
modules:
  - trusted_actions
  - evals
  - documentation
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/trusted_actions/test_public_error_leakage.py
  - tests/trusted_actions/test_plan_error_boundaries.py
  - tests/governance/test_cli_console.py
  - tests/governance/test_contract_import_boundary.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 0.9.4 安全治理整改阶段验证报告

## 1. 结论、范围与完成边界

本报告冻结可复现SBOM、治理CLI中文输出、计划错误信任边界，以及合同/执行导入隔离的阶段证据。
候选实现Revision为`21b5eb1d57055f32ba2178c165b3ad46060ee7c7`，但完整本地回归及已观察CI对应
其前序Revision `a5fd57eda953ba9f04f8e4673d1432306adac6a9`。两组版本的证据不得混用。

- a5fd57e完整已提交代码本地回归：**3904 passed、32 skipped、0 failed**。
- 21b5eb1合同导入隔离：16条控制台/导入专项、214条Eval及相关专项、107条治理/计划错误回归通过；
  测试组有重叠，不能把三组数量简单相加作为不重复用例数。
- a5fd57e的CI五作业通过，Windows剩余两项合同生成器加载POSIX执行依赖的失败；历史失败不改写为成功。
- 21b5eb1为两项Windows失败提供已求证的九个执行导出隔离整改；修复版跨平台CI终态尚未纳入本报告。
- 本报告为**专项候选证据，不是0.9.4、0.9或1.0正式发布PASS**。Secret扫描、许可证来源等独立缺口仍阻断发布。

未提交的`tests/security`攻击草稿仍有不可用接口，不作为正式验收证据；3904条通过是已提交代码的回归，
不是含该草稿的工作树`make check`成功记录。本轮真实Provider网络请求为0。

## 2. 需求背景、设计与源码追踪

| 缺口 | 源码/设计 | 已完成的有限整改 |
|---|---|---|
| 被忽略SBOM在干净Checkout缺失，规范字段不合法 | [SBOM专项详设](../../changes/m09-4b-reproducible-sbom.md)、[生成器](../../../scripts/sbom_generate.py) | 固定版本化库存、官方Schema/来源原件、Package URL/Archive/图身份、只读漂移检查。 |
| Windows管道cp1252无法写中文 | [公共控制台边界](../../../scripts/cli_console.py)、[回归](../../../tests/governance/test_cli_console.py) | 五个CLI共用UTF-8流重配，Reader显式解码，两种入口正例与失败stderr保持原退出码。 |
| KernelError被当成内容公开资格 | [计划错误详设](../../changes/m09-4a-plan-error-trust-boundary.md)、[固定码边界](../../../src/harnessix/trusted_actions/public_errors.py) | 按decode/resolve/policy阶段重建固定消息，未知码失败关闭，原retry提示不透传，显式Decoder异常收口。 |
| 合同Generator加载POSIX Eval执行代码 | [导入边界详设](../../changes/m09-4b-governance-contract-import-boundary.md)、[Eval包入口](../../../src/harnessix/evals/__init__.py) | 延迟九个真实执行导出，保留原对象/类型/公共名称，不注入假fcntl或广告Windows执行支持。 |

## 3. 固定环境、预算与验证方法

本地环境/解释器及实际命令见[verification.json](verification.json)。真实Windows证据来自
[CI 36284121012](https://github.com/carrie1988/Harnessix/actions/runs/36284121012)和
[CI 36285345506](https://github.com/carrie1988/Harnessix/actions/runs/36285345506)，不是本机模拟代码页的替代描述。

计划边界使用真实Agent Runtime、ScriptedProvider历史、SQLite Session、Action Audit、Protocol Server/SDK及
OpenTelemetry内存导出。可恢复参数拒绝必须实际进入下一次模型请求历史；未知/跨阶段码结束只读Turn。
代码页测试在新进程强制cp1252:strict及关闭Python UTF-8模式，再由CLI显式重配输出。
合同导入测试在新进程禁止六个执行模块，合同、名称发现、help/check必须独立通过；不存在POSIX模块时不跳过这些测试。

没有调用模型API、连接用户远端服务器或执行真实外部写操作；测试中的Provider为确定性内存事件。
所有故障式样在内存构造，不存原异常正文、Prompt、argv或真实凭据。

## 4. 本地测试结果与版本分离

| Revision/验证 | 结果 | 证据意义 |
|---|---|---|
| a5fd57e全部已提交测试 | 3904 passed / 32 skipped，347.88秒，退出0 | 前序完整代码回归；不宣称包含后续Eval导入隔离源码。 |
| a5fd57e治理专项 | 77 passed | 当时治理契约、字节/来源、规范与编码用例。 |
| a5fd57e公开错误及控制台专项 | 38 passed | 当时五公开面/固定码、取消/回调超时及代码页边界。 |
| 21b5eb1导入/控制台 | 16 passed | 三个新进程负例、九对象POSIX身份及原12个编码/流用例。 |
| 21b5eb1 Eval及相关专项 | 214 passed | 原Eval数据/执行/交付回归与相关专项；有数量重叠。 |
| 21b5eb1治理及计划错误 | 107 passed | 导入隔离没有破坏既有治理/规范或公开错误回归。 |
| 21b5eb1源码门禁 | Ruff/format、Mypy 328文件、Readability、合同、Task Pack、当前供应链命令通过 | 供应链命令通过不代表规则覆盖或许可证证据已经完整。 |
| 修改文档图表 | 实际Mermaid渲染通过 | 不仅依赖围栏语法和静态结构检查。 |

第一次本地完整回归在新设计的必需章节检查处有一个失败；补齐正式接口/错误分类章节后重跑，
得到表中的3904/32结果。失败不是通过忽略该治理测试修复。运行日志只记录Hash供溯源，未在本目录发布原文。

## 5. CI失败、根因与修复谱系

| CI/源码 | 冻结事实 | 后续修复 |
|---|---|---|
| 36129964234 / 880c306 | 多平台因文档链接到未提交dist/SBOM失败；本机残留不能证明干净Checkout。 | b06396a固定governance库存与离线Schema，并修复格式/自检字面量。 |
| 36284121012 / b06396a | 五作业成功，Windows 5 failed / 537 passed / 45 skipped，均为中文管道编码。 | 78ab300统一五CLI输出UTF-8并明确父进程解码。 |
| 36285345506 / a5fd57e | 五作业成功，Windows 2 failed / 572 passed / 45 skipped，原编码失败消失，Generator两入口加载fcntl。 | 21b5eb1把平台无关合同与九个实际执行导出分离；真实修复版CI另行核验。 |

最终a5fd57e作业元数据见[ci-observation.json](ci-observation.json)，只保存结果、精确源码、Job/Run身份和URL，
不复制CI环境或日志。此文件是观察记录，不是GitHub签名证明或修复版新CI结果。

## 6. SBOM、归档及原字节复核

[sbom-facts.json](sbom-facts.json)记录73个第三方组件、74个图身份、131条边、777个发行Archive引用，
以及Lock、Project和规范库存的SHA-256。库存SHA为
`daa484faf46cdd85fc1f485f817d326f0be39866b1802a9af6e73117dc6bb380`。

通过实际提交的git archive重建最小检查目录，复制SBOM脚本、公共控制台、锁/项目、基准和固定Schema，
在无dist且未安装自有项目的目录运行直接脚本；cp1252:strict环境仍复算同一库存SHA。
Windows自动换行Checkout的独立复算也验证一致；该模拟证明字节纪律，不单独证明Windows平台所有功能。

这仍是全平台/Extras/开发组的pre-build锁定库存，不是实际安装环境、Wheel或Container内容SBOM，
也不证明许可证完整、无漏洞或供应链来源签名。

## 7. Manifest、Review Packet与独立复核

交付文件集中在当前目录：

- [完整命令和验证事实](verification.json)；
- [SBOM固定事实](sbom-facts.json)；
- [CI最终观察](ci-observation.json)；
- [Review Packet](review-packet.json)；
- [文件Manifest](bundle-manifest.json)。

Manifest列举本目录文件的路径、字节数与SHA，不包含Manifest自身，避免自引用。
本目录固定原字节，Windows不做自动换行转换；[治理回归](../../../tests/governance/test_security_governance_evidence.py)
核对相对路径、文件Hash及对应源码Revision中的外部输入字节。
Hash只证明记录后未变化，不代替独立执行或来源认证。

## 8. 未完成门禁、风险与后续工作

1. 修复版Windows及其他CI终态尚未作为候选通过证据，不得因POSIX回归通过关闭全部阶段。
2. SEC-094-B2仍已复现：压缩Wheel成员、示例文件、含NUL二进制和缺失文件均可能错误地返回零命中；
   须完成有界不落盘归档读取、取消整文件豁免、读失败关闭和每条规则正反例。
3. SEC-094-B3许可证报告仍未绑定锁定版本、来源与证据，版本变更不得继承旧许可结论。
4. 0.9.4a其他异常/结构化Outcome/扩展输出、编号攻击套件和远端MCP均需继续审查与验收。
5. 0.9.5真实安装/Beta、0.9.6平台/Provider发布证据独立；合同可导入不表示Windows Eval执行全链已完成。

所有未完成项见[总体详设](../../changes/m09-4-security-and-supply-chain.md)与[路线图](../../roadmap.md)，
本目录候选记录不修改总体完成门禁。
