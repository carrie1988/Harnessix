---
doc_type: validation-evidence
status: current
version: 2
code_revision: 5402621ab7700f55382d78fa95681dfe3485c960
owners: [core]
modules: [agent, artifacts, processes, secrets, product_config, trusted_actions]
related_adrs:
  - docs/adr/0097-typed-binary-publication-and-owner-protection.md
related_tests:
  - tests/artifacts/test_binary_publication.py
  - tests/processes/test_output_protection.py
  - tests/product_config/test_process_action.py
  - tests/product_config/test_publication_scope.py
  - tests/trusted_actions/test_publication_recovery.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 正式二进制公开与Owner持久前保护验收报告

## 1. 固定范围与总体结论

源码及正式功能测试绑定`5402621ab7700f55382d78fa95681dfe3485c960`，生产实现提交`85acdf0`；后者至固定源码仅增加治理证据测试。
[总体与详细设计](../../changes/m09-4a-typed-binary-output-publication.md)提供四幅图、完整流程、接口、字段、伪代码与源码链接。
按正式用途解码原Base64双流，检查前缀位移及同流跨Chunk；模型原材料只传私有Owner保护封套，
目标环境及批准计划不增加注入权限；Owner先脱敏再计量/Hash/持久；拒绝正文后保留已验真效果而不再执行。
本报告不关闭0.9.4a、0.9.4及0.9整体。许可证门禁12件仍失败，不称make check通过。

```mermaid
flowchart LR
  Baseline[干净旧版归档与实际消费链缺口] --> Design[正式用途与独立保护边界]
  Design --> Source[固定源码与测试合同]
  Source --> Local[真实本机Owner及产品合同替身]
  Local --> Artifact[干净Wheel和sdist检查]
  Artifact --> Regression[稳定测试树完整回归]
  Regression --> Review[版本绑定报告与发布阻断项]
```

## 2. 源码研究与独立旧版负例

干净`d09f58d` Git归档复跑原正式产品Process测试探针，审批、Router、Agent、SQLite、Artifact分页链真实；
Owner为正式Lease合同替身。结果为Turn completed、两条Artifact记录，解码页包含登记合成值，
原页文字不含原值，Run=1/Reconcile=0、提交确认丢失按原身份恢复。
观察保存于[合同事实](contract-facts.json)，探针源码转换Hash保留；不是从新代码关闭检查或伪造旧版本。
旧报告及Manifest均不修改，既有4749项通过记录不冒充本实现验收。

## 3. 正式合同与实现验收

- `action_output`与旧只读`process_output`分别使用既有正式解析器；任意同名Base64字段不自动解码。
- 原JSON和正式解码共用工作计数、取消和10秒期限；解码最多双流、聚合1MiB，不强制UTF8。
- 每流跨12KiB Chunk检查；stdout和stderr不合并。偏移、Hash、Base64或用途篡改拒绝。
- Owner v2私有封套原材料最多32项/64KiB，编码池在解码前限制，完整帧1MiB，联合模式最多256。
- 父进程在Lease.create与Owner spawn之前校验；原Start v1 Schema及所有旧公开Schema不改，没有新数据库迁移。
- 模型原快照进入实际Supervisor保护端口，原Process Secret Provider和批准的环境目标独立。
- 已终态动作拒绝公开正文后恢复只返效果元数据；无output、无Artifact引用/Hash，不改Audit、不再Execute/Reconcile。

## 4. 测试设计与实际执行

专项 **108 passed、3.50秒**：91项新增功能用例及17项既有用例，另外新增两项治理证据用例。
相关回归 **1254 passed、5 skipped、51.22秒**。专项和相关回归均与完整回归重叠，不加总。
两份新增文档五幅Mermaid已实际渲染并逐幅视觉检查，不声明重新渲染全部既有图。
真实本机Owner三场景分别是退出、超时与取消，检查非UTF8前缀、同流逐字节输入、脱敏后持久文件/Hash/Lease/回执；
取消用真实子进程就绪标记同步，不用固定sleep推定已经输出。
产品Process四场景使用实际审批、Router、Agent、SQLite和安全正文分页，Owner为合同替身；
泄漏拒绝没有Artifact行和后续模型请求，确认效果及原Audit保持，终态再次resume不重执行。

完整回归 **4842 passed、32 skipped、401.51秒，exit=0**。
稳定测试提交`b1c173845775a65e223bd8aab7ce091dcd82e951`，Git Tree `f01cfc6b6f4aa64da66bb87fe7935b2a2e2e612a`；
2353个跟踪输入在运行前后Hash与状态均一致，未跟踪安全草稿不参与。
所有生产源码、合同、脚本、政策、基线与全部测试同固定源码；测试树只增加设计和验证种子文档。
93项新增由91功能用例及2治理用例构成；专项/相关计数不与完整回归相加。
原4842项结果不代替后续修改后的源码验收。
初期相关运行4 failed、1225 passed、5 skipped、51.71秒：恢复代码错误地从effect origin推定恢复策略，
以及测试引用不存在的probes字段。修复为显式内部策略和实际probe_cache后重新验证；原失败不追认通过。
恢复码白名单只适用于output，其他阶段继续固定归一；错误文本不透传合成值。

## 5. 构建、供应链与平台边界

Wheel/sdist从固定源码干净Git归档离线构建，文件Hash见合同事实。发行物不含未跟踪安全草稿。
这是源码版本绑定构建，不声明跨环境二进制可重现构建；代码规模/依赖关系/政策以固定输入验证。
实际本机测试不证明Windows Job/ConPTY、真实容器引擎、安装/升级/卸载或网络模型请求。
模型工厂和产品SDK测试使用ScriptedProvider及受控传输驱动，不是完整产品字节级stdio验收。
许可证门禁仍有12件Archive阻断，原拒绝政策不放宽。

## 6. Manifest、Review Packet与复核命令

本目录为单一交付入口：
[Manifest](bundle-manifest.json)、[合同事实](contract-facts.json)、[验证记录](verification.json)、
[评审包](review-packet.json)、[CI快照](ci-observation.json)及本报告；Manifest自身不自包含Hash。
Manifest精确绑定43个源码/测试/Schema/政策输入；每个输入从固定Git对象核对，不用工作目录当前版本替代。

```bash
uv run python scripts/generate_specs.py --check
uv run python scripts/readability_report.py --check --check-final-report --quiet
uv run pytest -q -o addopts='' --ignore=tests/security tests/governance/test_security_governance_evidence.py
uv run pytest -q -o addopts='' --ignore=tests/security
```

CI后台运行，不逐个本地提交等待、不主动重跑；冻结时本实现CI尚未启动，旧版本快照不是当前实现验收。

## 7. 开放风险与下一阶段

历史Session/其他公开出口、跨重启原材料证明与正文安全恢复、Owner/Store同步资源归属、
TM编号化攻击与三平台证据、远端MCP身份/OAuth/出口、真实安装/Beta、真实Provider发布/成本及许可证12件仍开放。
无保护独立宿主仅保持兼容，不能证明默认产品端到端安全；未知编码与跨流推断不在当前解码合同内。
未新增真实模型请求或定时任务；总目标及路线图完成条件保持不变。

## 8. 已确认的下一公开边界：模型直接文本

固定源码的独立组件探针注册同一模型材料类型的合成值，两个Scripted响应；第一回复直接输出登记值，
第二回复为安全文本。两Turn completed，值进入实际Session事件、SQLite文件、SDK事件回放和第二次模型请求历史。
SDK Thread摘要未包含值；六个非空OTel Span及Metrics未命中，不由此宣称全部遥测或全部公开面安全。
探针使用实际Runtime、SQLite、SDK和应用服务，非完整产品启动、字节级stdio或真实网络模型验收。
[合同事实](contract-facts.json)保留独立观察、可执行组件探针及源Hash；不将探针加进108专项或4842回归统计。
初次探针遗漏应用服务所需Store参数，修正为真实签名后完成；初次API错误不当作公开边界业务结果。

这一缺口与本切片修复的ToolResult/正式Process Artifact边界分开：模型TextDelta/完成文本、
用户输入、Context、Compaction及历史出口还需要按来源、授权、同一原材料、流式边界和持久前校验独立设计。
不得只在最终UI或单次回复上事后过滤，也不得重签旧历史或用当前环境新值追认原材料。
下一切片先研究正式事件提交与模型历史装配，再定义取消、预算、恢复和流式发布合同；整体发布继续阻断。
