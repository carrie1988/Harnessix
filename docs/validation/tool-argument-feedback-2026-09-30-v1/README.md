---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 007839e7648b6fc1b94a5acede8dd0285092249b
owners: [core]
modules: [tools, models, agent, artifacts, processes]
related_adrs:
  - docs/adr/0050-model-correctable-tool-validation.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_argument_feedback.py
  - tests/tools/test_paging_feedback.py
  - tests/tools/test_scoped_runtime.py
  - tests/artifacts/test_runtime.py
  - tests/processes/test_child_ready.py
  - tests/tools/test_windows_git.py
supersedes: []
---

# 严格工具参数反馈、原Session诊断与原生就绪验证

## 1. 结论与适用范围

正式只读Runtime保持严格Schema、Scope、批准及Artifact归属检查，只在原参数校验失败后，
从已注册模型字段生成必填、缺少及允许字段提示。没有自动补值、自动重试、Tool Alias变更、
模型配置/Prompt修改、预算扩大、Task Pack或评分器修改。

18项新测试在固定原实现的隔离解释器中全部RED；新实现关联1907项中1887通过、20项平台跳过，
失败与错误均为0。新测试包含两种正式Provider适配器、三种错误输入的6条SDK→Kernel→实际搜索
Artifact发布→失败提示→新调用→实际读取→资源关闭→Session重开/Replay链。MockTransport不访问网络，
该结果不能证明真实模型质量提高或完整消费者Windows验收。

产品修复的实际源码SHA、回归原件及CI分组事实由[Facts](facts.json)固定。
头部Revision是设计/原实现基线，不代替后继提交的实际文件身份。
完整架构、字段设计、数据流程、时序、失败及恢复见[详细设计](../../changes/m09-r3-safe-tool-argument-feedback.md)。

## 2. 原真实Session只读诊断

[白名单诊断](session-diagnosis.json)对应原`0813c58`、Suite
`d85c26ca-d21a-4ac4-9a01-f780eb01b038`的9份Session；SQLite以`mode=ro`读取，
事件经正式模型校验和纯Reducer回放，读取前后原Session字节摘要一致。

- 4次`read_artifact`失败均缺少正式必填`artifact_id`，这是本修复的直接依据。
- 47次宿主Context准备均包含编码指令v2；Profile已提供Artifact引用；模型历史会保留失败的error/output。
  宿主Context事实不等于供应商实际接收字节的独立证明。
- 存在路径找不到、检查阶段不完整及累计Token实际超限等独立失败，不能将所有Trial归因于字段提示。
- 末次Provider输出的具体解析原因仍未知：原正文没有保存，不推断其字段、名称或内容。
- 已完成8份报告为7份invalid、1份failed，第5个Case首Trial取消；没有完整20 Trial报告，
  不发布新的完整0/20成绩或合并其他版本的成功Trial。

该投影仅包含静态工具名/分类、正式字段名、参数额外字段数量、事件位置、Context预算和原件SHA。
`other`表示不在本诊断白名单中的代码，不表示已确认一种新根因。工具参数值、未知字段名、路径、
模型正文、Prompt、凭据与原Artifact内容均未导出。原历史FAIL见
[Suite中断证据](../provider-suite-interruption-2026-09-30-v1/README.md)，原请求预算及未知占用保持不变。

## 3. 失败保留、实现与兼容

原一般消息不能给出缺少字段。修复复用`model_fields/FieldInfo.is_required()`，按静态字段排序，
仅对正式必填键检查存在性；`null`、错型、越界和额外字段继续失败。消息不输出ValidationError或错误位置。
专用分页`tool_expected_revision_required`、失败类别、`retryable=false`与原工具Fingerprint不变。
旧Session保持旧消息；新调用重新执行严格校验，不能覆盖原失败事实。

初轮测试包含两项夹具错误：Glob有默认pattern，省略并非非法；OpenAI帧的response_id必须一致。
它们已依据正式契约修正，未改产品校验。原初轮FAIL私有保存；正式RED用修正后的同一组18项测试和
从固定Git Revision载入的原Runtime，在隔离解释器执行，不替换工作树或影响其他测试进程。
一次中间实现导致既有热点类增长，后继复用返回code/message的纯函数，原治理上限未放宽。
最终关联回归绑定最后源码，不能用中间通过或有缺陷夹具替代。

完整治理首次297项中1项失败：系统Apple Git 2.24.3没有使用测试自有`GIT_CONFIG_GLOBAL`夹具，
负对照未生成要求的CRLF；同一夹具在已固定Git 2.53.0下生效，原单项和完整297项均通过。
这属于测试工具链差异，不是产品反馈或Schema故障；未修改全局Git配置、测试断言或治理策略，
原FAIL及低敏配置探针保留。最终关联也在固定Git环境独立复验。

## 4. 原生候选与不相加的验证范围

固定`007839e`的[原生CI运行](https://github.com/carrie1988/Harnessix/actions/runs/36627916447)
与本字段反馈后继源码分开记录：该候选验证上一轮两文件测试就绪合同，不包含本轮产品反馈修复。
双Python功能回归各5825通过、116跳过；最终Job因既有许可证检查失败，不是全绿。
macOS核心3861通过、88跳过；容器焦点19通过。各组存在重叠，不相加为独立产品用例数。
Windows结果与低敏原生发布探针以Facts最终观察为准；前置步骤成功不等于完整Job成功。

本机就绪合同4通过、13平台跳过，原关联293通过、25跳过，属于固定`007839e`范围；
本轮反馈1887通过、20跳过属于后继文件身份。新回归加入Windows原生工具焦点步骤，必须取得后继结果，
不能继承上一候选的原生通过。

Mypy、全树Ruff、冻结Schema、可读性、资料链接/实际变化Mermaid渲染、完整治理回归和实际Wheel/Secret检查
分别记录于Facts及[Verification](verification.json)。派生可读性报告更新，规则、上限和原历史基线不变。

## 5. 交付、恢复与Go/No-Go

[Review Packet](review-packet.json)和[Manifest](manifest.json)与完整报告位于同一目录；
Manifest绑定公开文件字节。原测试/CI日志及失败仅保留私有原件，通过SHA审计，不复制个人路径或原模型内容。
无数据库迁移、配置或安装依赖，随内部`1.0.0rc1` Wheel部署；不创建稳定Tag或商用Release。

**仅本字段反馈专项功能GO，商用1.0仍为No-Go。**
真实Provider费用核对、完整20 Trial严格质量、默认Desktop路径、消费者Windows11、独立Beta、
最终同候选原生矩阵及既有分发权利门禁继续开放。Docker原8容器运行事实不外推所有业务功能。
真实模型请求数为0，未覆盖旧失败、退款、新建周期、提高预算或降低验收标准。
