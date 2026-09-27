---
doc_type: change-design
status: reviewing
version: 24
code_revision: 7fd1187f07fa415ecf48211bc0149aff0d7a6191
owners:
  - core
modules:
  - trusted_actions
  - mcp
  - secrets
  - sandbox
  - product_config
  - documentation
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0073-mcp-catalog-binding-and-sandbox.md
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
related_tests:
  - tests/trusted_actions
  - tests/mcp
  - tests/secrets
  - tests/sandbox
supersedes: []
---

# 0.9.4 安全、许可证与供应链详细设计

## 1. 文档摘要

| 项目 | 内容 |
|---|---|
| 当前能力 | 威胁模型v2（TM-01～TM-13）、Trusted Action统一路由与审批、公开错误经KernelError稳定码传播、Gateway输出已脱敏、MCP本地stdio/in_process、Secret引用-解析-脱敏、AGPL+商业双许可治理文件、0.9.3六场景Soak证据链。 |
| 本文设计状态 | `reviewing`；a/b已有实现候选；0601ede六作业CI成功，Windows编码与POSIX合同导入污染修复通过。Secret v2在747fe9b CI五作业成功/Windows夹具失败，修复候选待验证；许可v2绑定777件，pywin32 12件受限正文冲突仍阻断，安装输入及c/d尚未验收。 |
| 影响模块 | Trusted Actions、MCP、Secrets、Sandbox、Product Config、构建/发布工程与文档治理。 |
| 关键ADR | [ADR-0066](../adr/0066-sandbox-network-and-secret-boundaries.md)、[ADR-0069](../adr/0069-unified-coding-action-risk-route.md)、[ADR-0073](../adr/0073-mcp-catalog-binding-and-sandbox.md)、[ADR-0064](../adr/0064-agpl-and-commercial-dual-licensing.md)。 |

0.9.4把"功能完整"收口为"攻击面有回归、供应链可审计、公开错误不泄漏"。路线图固定范围为：攻击测试、AGPL/商业双许可权利链、依赖和许可证扫描、SBOM、Secret扫描、安装脚本与扩展来源审查、Trusted Action Runtime公开错误清洗与泄漏回归、远端MCP Streamable HTTP/OAuth（独立目标身份、凭据生命周期、受管出口）。本切片按a～d四个连续纵向子切片实施，任何子切片不得绕过前置项。

## 2. 需求背景

0.9.1～0.9.3证明功能与可靠性，但发布级安全证据仍是点状的：公开错误清洗分散在Gateway输出与通用异常兜底中，缺乏统一合同与逐边界泄漏回归；依赖、许可证、SBOM与Secret扫描没有版本化证据；威胁模型的控制多数只有单元级测试，没有按TM编号的攻击回归套件；远端MCP只留了合同枚举。供应链与安全边界见[威胁模型](../threat-model.md)与[MCP模块设计](../modules/mcp.md)第0.9.4目标行。

## 3. 设计目标与非目标

### 3.1 目标

1. **0.9.4a 公开错误清洗统一化**：Policy/Executor/Reconcile异常在全部公开边界（模型ToolResult、Session失败、Action Audit结果、Agent Protocol错误、遥测标签）只产生稳定公开码；注入含路径/argv/Secret式样/内部异常正文的故障，逐边界断言不泄漏。
2. **0.9.4b 依赖、许可证与SBOM**：版本化依赖清单与许可证审计、CycloneDX SBOM、仓库与产物Secret扫描、安装脚本与扩展来源审查、AGPL/商业双许可权利链复核，全部产物进入CI门禁。
3. **0.9.4c 攻击测试套件**：按TM-01～TM-13建立编号化攻击回归，覆盖威胁模型第6节每组威胁的当前控制；失败关闭语义可复现。
4. **0.9.4d 远端MCP Streamable HTTP/OAuth**：独立目标身份（固定origin/证书摘要/不允许隐式宿主凭据）、OAuth凭据生命周期（获取、存储引用、刷新、撤销、失败关闭）、受管出口（复用Sandbox Egress合同），离线契约与故障注入测试，默认产品不装配。

### 3.2 非目标

1. 不恢复任何已删除的Action HTTP/Worker面；远端MCP不成为第二公共控制面。
2. 不把扫描工具输出当作形式化安全证明；所有扫描结论限定于固定工具版本与规则集。
3. 不在本切片实现远端Skill市场、任意Header注入、公网监听MCP Server或自动更新通道。
4. 不为通过扫描而降低既有Policy、Approval、Sandbox或Secret边界。

## 4. 约束、假设与术语

| 项目 | 定义 | 影响 |
|---|---|---|
| 公开错误 | 可跨模型ToolResult、Session、Audit、Protocol与遥测传播的错误码与消息 | 仅白名单稳定码与固定消息；任何原始异常文本、参数值、路径、Secret不得进入。 |
| 泄漏回归 | 注入含敏感式样（伪Secret、绝对路径、argv、内部异常正文）的故障并扫描全部公开面 | 任一公开面出现式样字节即失败。 |
| 编号化攻击回归 | 测试名绑定TM编号（如TM-02、TM-07） | 新增威胁控制必须补对应编号测试。 |
| SBOM | CycloneDX JSON，固定工具与输入（uv.lock） | 版本化产物；依赖变更即漂移失败。 |
| 目标身份 | 远端MCP的规范origin（scheme/host/port/path）+ TLS证书SPKI摘要 | 与凭据作用域绑定；身份漂移失败关闭。 |
| 受管出口 | 远端MCP流量只经Sandbox Egress合同允许的目标 | 不走隐式代理/环境凭据；无Egress证据不装配。 |

## 5. 总体架构与数据流

```mermaid
flowchart TB
    subgraph A[0.9.4a 公开错误]
        EX[Policy/Executor/Reconcile异常] --> CL[统一公开错误合同]
        CL --> TR[ToolResult/Session/Audit/Protocol/Telemetry]
        LEAK[泄漏回归注入器] --> TR
    end
    subgraph B[0.9.4b 供应链]
        LOCK[uv.lock] --> SCAN[许可证扫描]
        LOCK --> SBOM[CycloneDX SBOM]
        REPO[仓库与产物] --> SEC[Secret扫描]
        LIC[AGPL/商业权利链] --> AUD[审计报告]
        SCAN --> GATE[CI门禁]
        SBOM --> GATE
        SEC --> GATE
    end
    subgraph C[0.9.4c 攻击回归]
        TM[TM-01～TM-13控制] --> ATK[编号化攻击测试]
    end
    subgraph D[0.9.4d 远端MCP]
        ID[目标身份+证书摘要] --> OAUTH[OAuth凭据生命周期]
        OAUTH --> EGR[受管Egress]
        EGR --> CONN[Streamable HTTP Connection]
        CONN --> GATE2[Trusted Action准入]
    end
```

## 6. 领域契约、数据结构、持久化与事务

| 契约 | 位置（规划） | 要点 | 失败语义 |
|---|---|---|---|
| 公开错误合同 | `trusted_actions/public_errors.py`（规划） | 固定公开码集合与消息模板；Executor/Policy/Reconcile异常一律收敛为`policy_denied`/`executor_failed`/`executor_unknown`/`reconcile_failed`等稳定码 | 未映射异常为`internal_error`，不携带任何原文。 |
| 泄漏式样集 | 测试夹具 | 伪Secret（`hxak-`前缀式样）、绝对路径、argv、内部异常正文 | 出现在公开面即测试失败。 |
| 许可证/SBOM报告 | `governance/`与`dist/sbom`（规划） | 版本化JSON；与uv.lock摘要绑定 | 漂移即CI失败。 |
| 远端MCP目标身份 | `mcp/contracts.py`扩展（规划） | origin规范形式+SPKI SHA-256+凭据引用 | 身份/凭据不匹配不建连。 |
| OAuth凭据事实 | `mcp/store.py`扩展（规划） | 仅持久化引用、到期、撤销与审计摘要，不存明文 | 过期/撤销/泄漏信号失败关闭。 |

持久化与事务：本切片不修改Session/Artifact/Action数据库Schema；远端MCP的凭据引用与目标身份沿用MCP SQLite Store的既有迁移纪律；扫描产物只写版本化文件，不写业务库。错误分类沿用`FailureCategory`与KernelError稳定码，不新增异常类型。

## 7. 核心流程

### 7.1 0.9.4a 公开错误流程

```mermaid
sequenceDiagram
    participant E as Executor/Policy
    participant R as Router/Reconcile
    participant C as 公开错误合同
    participant P as 公开面
    E--xR: 含敏感式样的异常
    R->>C: 映射为稳定公开码
    C-->>P: ToolResult/Audit/Protocol仅见公开码与固定消息
    Note over P: 泄漏回归注入器扫描全部公开面
```

### 7.2 0.9.4d 远端MCP时序

```mermaid
sequenceDiagram
    participant C as Catalog/Config
    participant I as 目标身份验证
    participant O as OAuth凭据
    participant G as 受管Egress
    participant M as Streamable HTTP
    C->>I: 声明origin+SPKI摘要+凭据引用
    I-->>C: 身份不匹配失败关闭
    I->>O: 按引用获取/刷新凭据
    O-->>I: 过期/撤销失败关闭
    I->>G: 出口仅允许目标origin
    G->>M: 建连与目录捕获
    M-->>C: Schema漂移检查后经Trusted Action准入
```

## 8. 接口设计

| 接口（规划） | 调用者 | 输入/输出 | 失败语义 |
|---|---|---|---|
| `public_error(exc, *, stage)`（0.9.4a） | Router/Policy/Reconcile | 任意异常 → 稳定公开码与固定消息 | 未映射异常为`internal_error`；绝不返回原文。 |
| `leak_fixtures`（0.9.4a测试） | 泄漏回归 | 四类敏感式样注入点 | 公开面命中即测试失败。 |
| `license_scan`/`sbom_generate`/`secret_scan`（0.9.4b脚本） | CI门禁 | uv.lock/仓库 → 版本化报告 | 漂移、命中或工具版本不符失败。 |
| `McpRemoteTarget(origin, spki_sha256, credential_ref)`（0.9.4d） | MCP Catalog | 目标身份与凭据引用 → 受管连接 | 身份/凭据/Egress不匹配不建连。 |
| `OAuthCredentialLifecycle`（0.9.4d） | MCP连接 | 获取/刷新/撤销/审计摘要 | 过期、撤销或泄漏信号失败关闭。 |

## 9. 源码与测试映射（规划）

| 设计元素 | 源码 | 测试 |
|---|---|---|
| 公开错误合同 | `src/harnessix/trusted_actions/public_errors.py`（规划） | `tests/trusted_actions/test_public_errors.py`（规划） |
| 泄漏回归注入 | `tests/trusted_actions/leak_fixtures.py`（规划） | 五公开面×四式样矩阵 |
| 供应链扫描 | `scripts/license_scan.py`、`scripts/sbom_generate.py`、`scripts/secret_scan.py`（规划） | `tests/governance/`对应正反例（规划） |
| 编号攻击回归 | 既有`tests/`下按TM编号聚合 | `tests/security/test_tm_*.py`（规划） |
| 远端MCP | `src/harnessix/mcp/`目标身份、OAuth、Egress扩展（规划） | `tests/mcp/test_remote_*.py`（规划） |

## 10. 失败、错误分类、兼容与观测

- 0.9.4a：公开错误只含稳定码；泄漏回归注入失败不改动业务事实，只证明公开面不含式样。
- 0.9.4b：扫描器自身版本与规则集版本化；工具更新产生的新结论经评审更新基线，不静默放行。
- 0.9.4c：攻击回归只操作临时夹具与离线替身；不触碰真实网络、真实凭据或用户Workspace。
- 0.9.4d：远端故障（DNS、TLS、令牌过期、服务器漂移、Egress拒绝）一律失败关闭并保留稳定错误码；UNKNOWN调用只经既有MCP/Trusted Action对账语义处理。
- 可观测信号沿用低基数标签；公开错误码进入既有错误分类，不记录式样。

## 11. 安全与隐私

扫描产物与攻击夹具不含真实Secret；伪Secret式样仅存在于测试夹具。SBOM与许可证报告不含私有路径。远端MCP凭据明文只存在于Secrets最小作用域，Audit只存摘要。

## 12. 测试与验收

1. 0.9.4a：泄漏回归覆盖五类公开面 × 四类敏感式样；既有Trusted Action/网关测试全部通过。
2. 0.9.4b：许可证白名单复核通过、SBOM可重生成且逐字节稳定、Secret扫描零命中且包含正例自检。
3. 0.9.4c：TM-01～TM-13每组至少一个编号攻击回归在CI三平台通过。
4. 0.9.4d：离线契约（目标身份、凭据生命周期、Egress、Schema漂移、UNKNOWN对账）与故障注入全部通过；默认产品不装配的证明测试。
5. 每个子切片完成须同步现行模块设计与威胁模型链接，`make check`全链通过。

## 13. 风险、限制与后续工作

### 13.1 已复现的发布阻断缺口

| 编号 | 当前行为与可复现边界 | 必须完成的整改 |
|---|---|---|
| SEC-094-A1 | 原实现按类型透传任意KernelError；已由三个失败负例复现。当前候选按decode/resolve/policy有限合同重建固定错误，不继承消息/retry提示。 | [专项详设](m09-4a-plan-error-trust-boundary.md)及真实Runtime/Model历史/Session/Audit/Protocol/Telemetry回归已建立；本地完整回归3904 passed/32 skipped及五公开面专项通过；0601ede六作业CI成功已验证该窄计划边界；0.9.4a的Execute/Reconcile及结构化输出全面审查仍独立开放。 |
| SEC-094-A2 | Context Factory、审批Review与终态Output直接透传回调异常；四条调用路径×四类异常共16项负例失败。 | [Gateway专项详设](m09-4a-gateway-callback-error-boundary.md)建立阶段有限合同、取消传播和不变Audit终态；新增57项直接/取消/真实Runtime/执行对账集成回归。只治理抛出的异常，不关闭结构化Outcome或正常返回正文审查。 |
| SEC-094-A3 | Executor直接返回的结构化失败结果只受码格式约束；离线真实Runtime复现未登记码进入Model历史/Session/Audit/Protocol回放，合成诊断正文进入Model历史/Session/Protocol。 | [结构化失败详设](m09-4a-returned-failure-boundary.md)及[ADR 0093](../adr/0093-kernel-owned-public-failure-contract.md)实现新Audit归一、正式Process/Eval合同、失败Provider返回再验证和旧链不改。[成功Owner投影详设](m09-4a-success-output-projection-boundary.md)与[ADR0094](../adr/0094-audit-bound-bounded-owner-projection.md)进一步治理成功回调双摘要、正式DTO、序列化前预算、期限和取消。[执行器原始返回详设](m09-4a-executor-output-budget.md)已补封套预算、后置期限、取消记账和只对账恢复。有效成功JSON公开授权、Owner内部预算和其他边界仍开放，不等于0.9.4a完成。 |
| SEC-094-B2 | v1对NUL二进制和`.env.example`跳过、压缩Wheel不读取成员，缺失输入静默放行；四项负例均失败。v2已删除skip路径，新增有界ZIP/TAR/压缩流、BOM视图与固定未完成合同。 | [有界扫描详设](m09-4b-bounded-secret-scan.md)覆盖坏包、隐藏尾部、预算、取消/超时及真实包门禁；[冻结报告](../validation/secret-scan-2026-09-27-v1/README.md)记录3996 passed/32 skipped、87专项与干净源码Wheel/sdist；747fe9b CI五成功/Windows夹具失败，[夹具详设](m09-4b-secret-fixture-portability.md)保留旧失败并补身份自检，候选仍需新Windows终态；不标记整体0.9.4b完成。 |
| SEC-094-B3 | [`build_report`](../../scripts/license_scan.py)只按包名读已安装元数据，不存锁定版本。相同包名的`0.0.0`与`999.0.0`两个锁输入生成相同报告。 | [Archive级许可详设](m09-4b-archive-license-evidence.md)建立777件实际元数据/通知原字节、锁版本/来源/摘要绑定和离线SPDX/收据门禁。实际pywin32 12件包含LGPL正文复核信号，现有拒绝策略保持；替换或例外及义务评审未完成，发布仍阻断。 |

上述行为是离线合成输入已确认的控制缺口，不表示发现或保存了真实凭据；尚未通过端到端回归的
整改不得标记关闭。SBOM格式与干净检出整改只解决库存交付，不替代这些独立安全门禁。

- 扫描与攻击回归覆盖的是已知模式，不证明无未知漏洞；1.0发布仍需0.9.5安装证据与0.9.6 Provider证据。
- 远端MCP的公网真实联调需要受控目标与凭据，属0.9.5 Dogfooding候选，不阻塞本切片离线合同。
- 0.9.5受控Beta与0.9.6真实Provider凭据是外部依赖，届时逐项登记。

## 14. 变更记录

0.9.4b的干净检出、CycloneDX格式和版本化输入整改见[可复现SBOM专项详设](m09-4b-reproducible-sbom.md)。
该整改不替代后续许可证来源、发行物Secret扫描、攻击回归或远端MCP验收。

| 文档版本 | 代码版本 | 日期 | 变更摘要 |
|---|---|---|---|
| 1 | `668598220aa0cf328cb2e678bf16661550423804` | 2026-09-25 | 建立0.9.4总体设计与a～d子切片分解；实现与验收待完成。 |
| 2 | `668598220aa0cf328cb2e678bf16661550423804` | 2026-09-25 | 0.9.4a实现：新增`trusted_actions/public_errors.py`统一公开错误合同；计划阶段Resolver/Policy异常收敛为`action_plan_failed`，执行/对账异常映射统一收口；新增五公开面×四式样泄漏回归；Trusted Actions模块设计同步。 |
| 3 | `7a39f28bfb0d82e75a9e7a8b677c642d7733f845` | 2026-09-25 | 0.9.4b实现：许可证白名单扫描（74包全过，平台条件包登记overrides）、CycloneDX SBOM漂移门禁、Secret扫描（零命中+正例自检）、CI动作全部SHA固定、httpx2/httpx2-jsfetch补登记、许可证权利链与安装/扩展来源两份审查文档；make supply-chain纳入check链与CI。 |
| 4 | `880c3065482c00d4b0761c739c3ff94f7a7d00cb` | 2026-09-27 | 登记旧CI的未跟踪SBOM与文档链接失败；整改候选增加版本化pre-build库存、固定上游Schema、归档/图身份和干净检出回归；不追认旧实现完成。 |
| 5 | `78ab30069ab4f63d58d9bafad903a7bfd660c257` | 2026-09-27 | 登记库存首轮CI的Windows管道编码根因及UTF-8整改；按阶段有限合同修复KernelError类型信任和显式Decoder异常边界，补源码绑定专项详设及五公开面端到端回归。 |
| 6 | `a5fd57eda953ba9f04f8e4673d1432306adac6a9` | 2026-09-27 | 本地完整回归及五公开面/治理编码测试通过；真实Windows剩余两项合同生成器fcntl导入污染，隔离九个Eval执行导出，不广告Windows历史执行支持。 |
| 7 | `21b5eb1d57055f32ba2178c165b3ad46060ee7c7` | 2026-09-27 | 固定阶段证据、当前源代码及候选导入隔离回归；0.9.4全部发布门禁保持未完成 |

| 8 | `0601ede74c01039411c8be85e4297debe9dff478` | 2026-09-27 | 登记前序六作业CI终态成功；Secret v2归档/失败关闭候选建立正式详设、四项缺陷负例及真实发行物门禁，不关闭其他子切片。 |

| 10 | 修复候选 | 2026-09-27 | 登记747fe9b真实Windows夹具失败；稳定短ID与ZIP双记录原名自检。许可门禁迁移Archive级v2、全部777件采集与离线重读；pywin32 12件保持实际许可发布阻断。 |
