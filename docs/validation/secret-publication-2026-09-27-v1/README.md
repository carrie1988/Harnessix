---
doc_type: validation-evidence
status: current
version: 2
code_revision: e730f4858c76dbbb614a81b1b3e12184c433266c
owners: [core]
modules: [secrets, trusted_actions, product_config, mcp]
related_adrs:
  - docs/adr/0095-versioned-secret-publication-scope.md
  - docs/adr/0094-audit-bound-bounded-owner-projection.md
related_tests:
  - tests/secrets/test_publication.py
  - tests/trusted_actions/test_secret_publication.py
  - tests/trusted_actions/test_secret_publication_runtime.py
  - tests/product_config/test_secret_scope_composition.py
  - tests/mcp/test_server.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 版本化Secret公开保护作用域验收报告

## 1. 范围、背景与结论

实现Revision：`e730f4858c76dbbb614a81b1b3e12184c433266c`。
[完整详细设计](../../changes/m09-4a-versioned-secret-publication.md)和
[ADR-0095](../../adr/0095-versioned-secret-publication-scope.md)说明需求、上游源码研究、方案取舍、
架构、流程、时序、数据流、类与接口、字段、伪代码、错误、取消、期限、恢复、安全和部署。

[前序独立观察](../custom-success-2026-09-27-v1/README.md)证明字段合同与Hash正确时，已绑定Secret值
仍可进入Session、下一模型请求和SDK回放。本次引入宿主显式版本化内存快照：仅解析登记绑定，
Product Process执行与公开检查使用同一实例；检查原摘要和Owner重建结果，拒绝正文而保留已确认效果。

专项 **62 passed，1.03秒**（56新增、6项原MCP回归），相关回归 **814 passed，15.81秒**，
两者与完整回归重叠，不相加。首次完整回归1 failed、4696 passed、32 skipped（397.12秒）发现取消历史计数误判；
修复后78项针对性回归通过，含4个新增取消基线用例。稳定测试树完整回归 **4701 passed、32 skipped，409.57秒，exit=0**。
测试树`8e47909ddfebde93d4243b13680a0b29e9ef7fd6`，Git Tree `dc0d22ccbb3beb1b54fc3e77af8237dd2acc872d`；跟踪输入运行期间无变化，生产源码、
Schema、脚本、政策与基线同实现Revision，全部测试同实现Revision，含两项本切片证据治理用例。当前验收限定于本次作用域，
不声明产品全量验收、全部Secret端到端安全、跨重启正文安全恢复或整体0.9完成。
真实模型请求0，无新增定时任务；未跟踪安全草稿不修改、不提交、不计作TM编号验收。

## 2. 独立旧版负例

在独立`58e51aa7d7ca42db8accb1b84b9911e589caf2d3`源码归档中复制当前测试，
只选不提供作用域的4项已有接口案例。未选新能力的导入被省略，实际Gateway模块路径核验为归档位置。
结果 **4 failed，7 deselected，0.26秒，exit=1**，全部为`DID NOT RAISE`，
并非构造或导入错误。当前同4项默认拒绝；其余显式作用域覆盖原值、Base64、URL和Hex。

旧冻结报告保持不变。本报告不把早期因新参数缺失导致的失败当作负例，不用当前目录冒充旧版本。

## 3. 执行、公开与恢复边界

```mermaid
flowchart TD
  Host[宿主显式登记name/version] --> Snapshot[有界原版本Secret快照]
  Snapshot --> Execute[Product Process解析同一实例]
  Snapshot --> Guard[Gateway或MCP公开检查]
  Execute --> Audit[保留确认效果与原Hash]
  Audit --> Guard
  Guard --> Native[原生键值与标量 / 规范JSON有限扫描]
  Native --> Safe[合法原JSON公开 / 不修改原Hash]
  Native -.命中或缺能力.-> Deny[固定错误 / 不重复执行]
  Audit -.仅Hash恢复且绑定Secret.-> Metadata[已核验效果元数据 / 无正文或Artifact / 不调用Owner]
  Snapshot --> Close[宿主正常或启动故障退出回收]
```

[Scope](../../../src/harnessix/secrets/publication.py)保存独立可清零副本，不枚举环境或落盘明文；
[纯保护接口](../../../src/harnessix/trusted_actions/agent_gateway_output.py)不增加trusted_actions到secrets
的具体依赖。Product组合根保证同一实例，独立SDK/MCP宿主须遵守同一执行与公开快照合同。
环境旋转不替换运行中已捕获值。Scope关闭后，Process在无Lease时归类确定前置失败，Run/Reconcile=0。

原生树、规范JSON和有限模式使用共用工作量及取消检查点；Owner发布前与重建后均检查。
[Gateway检查点](../../../src/harnessix/trusted_actions/output_budget.py)识别Token、期限和本次新增父Task取消；
异步入口交付待取消，已消费取消计数不阻断后续对账，不调用uncancel或读取私有Task状态，
终止时不启动Owner；实际取消不等于任意同步Provider或不协作Owner可被硬抢占。

只有Hash且绑定Secret的恢复，不以同名同版本新环境值授权旧正文。原Binding和Audit核验后仅返回
确定效果元数据，不公开正文或Artifact，不调用Owner，不重复Execute/Reconcile。
这是保留效果可观察性的降级路径，**不是跨重启旧Secret正文安全恢复**。

## 4. 用例、源码与证据适用性

| 文件及用例数 | 证据、边界与负例 |
|---|---|
| [Scope 30项](../../../tests/secrets/test_publication.py) | 键/字符串/标量、已有有限编码、规范JSON凭据、版本缺失、宿主异常、材料类型/长度、环境旋转、清零和关闭；原生循环/钩子/深度/节点/字节/共享工作预算。 |
| [Gateway 16项](../../../tests/trusted_actions/test_secret_publication.py) | 原值及编码×默认拒绝/显式快照共8项；Owner安全/命中/仅Hash恢复3项；受控时钟、Token和父Task取消3项，历史取消后内联/Owner发布2项。Audit保持成功，Execute=1、Reconcile=0，恢复不再执行。 |
| [实际Runtime 3项](../../../tests/trusted_actions/test_secret_publication_runtime.py) | 缺能力/命中/合法值，实际SQLite、已审批Action、非空SDK回放及OTel、下一Scripted请求和全部数据库字节；拒绝Turn失败但已确认效果成功，无第二次模型请求，恢复无重复。合成Executor和Scripted模型不代表真实Provider发布。 |
| [Product 4项](../../../tests/product_config/test_secret_scope_composition.py) | POSIX实际Executor+Scope/Router/Plan，旋转后仍解析原值；关闭后无Lease不Run。正常及启动失败组合根退出2项使用真实Gateway/Scope/SQLite及Container构造替身，不代表实际容器安装或Windows安装。 |
| [MCP 9项](../../../tests/mcp/test_server.py) | 6项既有回归，3新增实际Client安全/命中/Scope关闭；独立MCP Server有界字段+值检查，不借用Gateway结果，也不覆盖远端HTTP/OAuth。 |

[Product装配](../../../src/harnessix/product_config/action_composition.py)控制能力注入及失败清理；
[组合根](../../../src/harnessix/product_config/action_runtime.py)以AsyncExitStack注册回收，
在尚未创建AgentRuntime时也释放Scope；[Process前置失败](../../../src/harnessix/product_config/process_action.py)
只有确认没有Lease时归类确定失败；[MCP导出](../../../src/harnessix/mcp/server.py)独立检查原生DTO。

## 5. 静态、发行物与门禁

Ruff规则/格式、Mypy **337源文件**、生成合同、可读性/依赖门禁、SBOM及Secret自检通过。
既有生成Schema无漂移，无新增包、依赖边或环，不放宽可读性政策，不迁移Plan Binding/Audit。

干净实现Revision的git archive离线构建Wheel **380成员**与sdist **2305成员**；
原字节Hash见[事实](contract-facts.json)，均不含未跟踪安全草稿。
实际Secret扫描 **2327输入**完整覆盖、固定6规则零命中，不等于全部语义泄漏或位级可复现。

许可证门禁仍 **exit=1、12件受限Archive**；未放宽既有拒绝政策，不声明make check通过。
详细设计4图及报告1图已实际渲染为非空PNG并逐图目视复核；
文档门禁实际渲染变化文档通过，覆盖305份文档、8064条链接、735幅Mermaid、26个源码包。完整回归已登记，冻结后治理检查独立登记；
实际结果在[验证](verification.json)登记；32项跳过不等于三平台安装验收，不累计重叠测试数量。

## 6. 开放风险、CI与复核材料

有限模式不解决任意变形、拆分推断或未登记Secret。全部模型Provider凭据、历史Session和Artifact
公开治理、Owner/Store内部字节与归属、跨重启安全正文恢复仍开放。Python不可变内存不承诺擦除；
超界宿主材料不复制，也不进行超界全量清零，保留宿主所有权。Provider任意同步阻塞不承诺硬抢占。

### 6.1 模型凭据与Artifact历史的独立开放观察

在本实现Revision，按正式Product配置primary-api-key/v1由EnvironmentSecretProvider向
build_provider_bundle工厂提供合成值；实际SQLite Runtime使用该Bundle、CodingToolRuntime及Artifact Store。
测试Workspace的300条grep记录中，第150条人工放置同值，首个工具仅预览2条。实际SDK
artifact/read（offset149/limit1）返回该值；下一Turn合法read_artifact的结果进入持久Session、
下一Scripted请求历史和Protocol回放。首个预览、初始snapshot/回放没有该值，非空OTel仍无该值。
共4次Scripted请求，真实模型请求0；事实及8项版本输入Hash见[事实](contract-facts.json)。

这是公开边界缺口，不是验收通过。实验按组件装配，不代表完整Product stdio启动；值人工置于授权
Workspace，不证明真实凭据可被未授权读取。Gateway作用域不经过只读Tool/Artifact分页路径，
只保护Action绑定材料不足以关闭模型凭据与历史治理。

后续须让Provider实际解析与所有公开检查共享显式原版本材料，在Artifact全部正文发布前及分页出口
建立有界保护，不能仅扫preview。历史数据无原值证明时不得以当前同版本环境授权；需独立安全
证明/恢复设计，Append-only Session/Audit原账本不回写，取消、期限、归属和Hash仍按正式合同核验。
[Provider解析](../../../src/harnessix/product_config/runtime.py)、
[Product组合](../../../src/harnessix/product_config/server.py)、
[结果提交](../../../src/harnessix/agent/runtime.py)、
[Artifact Store](../../../src/harnessix/artifacts/sqlite.py)及
[SDK分页](../../../src/harnessix/app_server/artifacts.py)分别形成独立边界。

12件许可权利链、TM编号攻击证据、远端MCP身份/OAuth/出口、三平台实际安装/升级/卸载/Beta及
真实Provider发布/成本适用性仍阻断正式发布。32项历史跳过不能作为平台验收。

当前实现CI冻结时尚未启动；[CI观察](ci-observation.json)仅记录58e51aa运行的一次后台快照，
不是本次版本终态。统一推送后后台运行，不逐提交等待、不重复重启，也不以旧结果代替新验收。

复核材料集中本目录：[Manifest](bundle-manifest.json)、[事实](contract-facts.json)、
[验证](verification.json)、[Review Packet](review-packet.json)、[CI观察](ci-observation.json)。
Manifest记录5个交付文件及29项实现Revision输入Hash，自身排除；旧冻结材料不回写追认。
冻结后证据治理16项通过，Ruff/格式、合同、可读性与文档门禁均通过；版本输入和交付文件Hash再次独立复核。
