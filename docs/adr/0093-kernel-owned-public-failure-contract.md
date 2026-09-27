---
doc_type: adr
status: current
version: 1
code_revision: a0a136e8123bb2ed4db368917bcf61f218ab013f
owners:
  - core
modules:
  - trusted_actions
  - processes
  - artifacts
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
related_tests:
  - tests/trusted_actions/test_returned_failure_boundaries.py
  - tests/trusted_actions/test_process_failure_projection.py
  - tests/trusted_actions/test_returned_failure_runtime.py
supersedes: []
---

# ADR-0093：内核持有有限公开失败合同，Owner保留合法诊断归档

## 状态

接受。实施与完整接口、流程、失败语义见[结构化失败结果详细设计](../changes/m09-4a-returned-failure-boundary.md)。
本ADR不关闭整个0.9.4a安全切片，也不证明历史私有Audit或成功业务正文已经完成治理。

## 背景

执行器身份由宿主冻结，但返回值可能来自外部MCP、文件或数据库；格式合法的Outcome不是公开授权。
正常返回失败不进入异常sanitizer。Process/Eval同时需要合法诊断反馈，不能简单删除全部失败工件。
v1 Binding、计划Fingerprint及审批已持久化，添加可配置输出合同会改变旧指纹与恢复匹配规则。

## 决策驱动因素

1. 新Audit记录和公开Tool Result必须使用有限分类；不能接受模型、扩展或宿主动态注册任意错误正文。
2. 保留效果事实与外部身份，UNKNOWN不能因内容校验失败降级成已证明失败。
3. 保留Process/Eval正式元数据和Owner输出Artifact，不以安全整改破坏测试反馈链。
4. 不增加部署单元，不凭默认字段伪造v1指纹兼容。

## 候选方案

| 方案 | 优点 | 问题 | 结论 |
|---|---|---|---|
| 每个Binding持久化动态输出合同及Hash | 自定义合同灵活，计划能绑定策略 | 需要Binding/Route版本迁移、旧审批恢复策略及动态合同审查，当前并无开放自定义失败正文需求 | 不在本切片实现 |
| 只在UI清洗或仅使用Secret正则 | 改动少 | Audit、Session、模型历史仍暴露；未命中正则不代表公开授权 | 否决 |
| 删除全部失败正文与Artifact | 默认安全且简单 | 破坏Process/Eval测试诊断与恢复反馈 | 否决 |
| 内核静态有限策略与正式Process DTO | 默认拒绝、可回归、无需指纹迁移 | 新公开合同需要版本化源码改动；内核需要读取共享DTO | 采用 |

## 决策

Router在正常失败结果记账前应用源码持有的有限码表和正文策略，Gateway对旧终态及Provider成功
返回的失败投影再次验证。只有匹配宿主冻结来源身份、正式DTO、计划事实与审计摘要的Process/Eval
失败元数据可公开；实际诊断字节继续由既有Owner重建和Artifact Store所属域管理。

不变量：不改kind或external_action_id，不动态注册码，不借错误前缀授权，不重写旧Audit链，不自动
重执行，不改变Eval非零正常退出的SUCCEEDED业务结果，不捕获取消作为普通失败。

## 理由与依赖边界

Framework-agnostic约束是不依赖LangGraph等规划框架，不意味着Coding执行内核不能依赖正式工具
数据合同。源码有限策略避免策略配置被模型或扩展修改，也避免未经设计的v1计划指纹漂移。

明确批准两个**只读合同依赖**：trusted_actions→processes与trusted_actions→artifacts。
[`public_outcomes`](../../src/harnessix/trusted_actions/public_outcomes.py)只导入
[`PublicProcessOutputSummary / PublicEvalOutputSummary`](../../src/harnessix/processes/public_output.py)
与[`ArtifactRef`](../../src/harnessix/artifacts/contracts.py)，不导入Supervisor、Owner运行时、SQLite
Artifact Store、宿主组合根或产品配置。Process DTO只依赖ReadContract及ProcessStopReason等数据定义。
这不是把Owner、持久化或副作用搬进Router，也不新增调用入口。结构治理策略仅增加这两个有理由的
依赖边，不新增超大文件/函数豁免、不扩大复杂度阈值、不批准新的依赖环。

## 后果

### 正面后果

正常返回失败和抛出异常同样具备公开边界。正式诊断工件仍可恢复发布，归一不造成重复外部效果。
既有计划、批准、Store无迁移，当前阶段无需数据库或远程服务器变更。

### 负面后果与债务

自定义失败正文不再透传；宿主需要新公开合同应提交正式设计与码表测试，不能恢复任意JSON透传。
新源码只治理新写入与新公开投影；历史Audit和Session不自动净化。成功正文与返回值预算仍属后续
0.9.4a审查，不得以本ADR宣称完整安全。有限码表未来变更需要显式版本与回归，不是永久覆盖未知扩展。

## 兼容、安全与运维影响

Wire v1字段及Shape不变，但失败公开内容语义收紧；自定义调用方应按固定分类处理，而非依赖任意
内部码或失败JSON。升级无需变更指纹。回滚旧二进制会重新放宽公开边界，不属于安全的自动回滚；
恢复旧服务前应关闭受影响扩展并评审公开风险。既有Owner诊断权限、配额、分页与Artifact生命周期不变。
没有新增原异常日志或高基数Metric。当前代码Revision的CI异步运行，发布仍须对应Revision终态证据。

## 验证方式与关联资料

| 类型 | 路径 | 证明 |
|---|---|---|
| 完整设计与源码研究 | [详细设计](../changes/m09-4a-returned-failure-boundary.md) | 根因、合同、时序、计划身份与恢复语义 |
| 现行模块 | [Trusted Actions](../modules/trusted-actions.md)、[Processes](../modules/processes.md) | 持久事实与DTO的职责 |
| Audit与旧数据 | [边界回归](../../tests/trusted_actions/test_returned_failure_boundaries.py) | 新写归一、旧链不改、零重复执行 |
| Process/Eval及Provider | [正式投影回归](../../tests/trusted_actions/test_process_failure_projection.py) | 有效诊断保留、任意正文拒绝、摘要核对 |
| 实际五公开面 | [Runtime回归](../../tests/trusted_actions/test_returned_failure_runtime.py) | 实际后续模型历史与Session/Protocol/Audit/OTel |

## 被取代关系

不取代既有Action风险路由或双层Owner决策，补充公开内容边界。
