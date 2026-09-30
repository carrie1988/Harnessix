---
doc_type: validation-evidence
status: current
version: 1
code_revision: 940432fe238a37d546f0061809c1e9184e4faf01
owners: [core]
modules: [evals, models, product_config, trusted_actions, agent]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_reverification.py
  - tests/evals/test_provider_verification_host.py
  - tests/product_config/test_process_action.py
  - tests/tools/test_argument_feedback.py
  - tests/trusted_actions/test_plan_error_boundaries.py
supersedes: []
---

# R3 有界真实复验首个Trial中断证据

## 1. 固定范围与有限结论

新Suite在干净`940432f`上固定原Task Pack v2、3仓库10 Case/20 Trial、北京Coder快照和原12/20严格任务/必需测试门槛，
保留每仓严格成功、零越界、零损坏和零重复高风险效果要求。使用[已验证的有限显式同Engine宿主](../docker-eval-host-2026-09-30-v1/README.md)，
不计默认Docker Desktop认证。

[有界预算](../../changes/m09-r3-bounded-reverification-budget.md)已实际登记到原70元周期：
整个62项旧请求前缀不变，原unknown的20.77824元全额预留不结算、不释放；新Suite唯一授权和40元单轮上限可靠持久化后才读取凭据。
原来源检查、两镜像RepoDigest及新预注册先于首个付费请求。

**真实复验未完成。首个Trial中断，没有Trial/Case/完整Suite报告，也没有新的完整质量成绩。**
宿主进程退出1，仅公开`verification_host_failed`。持久Suite仍标为`running`，不是当前有活跃任务：
这是异常退出尚未形成完整进度终态的事实，不能手工改写为completed或当作运行成功。
历史完整0/20、前两次中断及原收费未决证据全部保留；不重试模型、不选择Trial或跨Revision拼接成绩。

## 2. 实际数据流与直接证据

```text
固定Revision及完整预注册
 → 原预算唯一有界授权与完整旧前缀核验
 → 9次发送前全额预留及完整已知结算
 → 原Agent读取文件并提出8次固定Profile调用
 → 8次参数都缺少必填profile；前7次返回tool_invalid_arguments
 → 第9次模型用量累计超过原50000 Token
 → 未结算的非只读调用经原保守收尾得到uncertain_effect，Turn为interrupted
 → 没有审批或可信Profile执行结果，没有Trial报告；顶层进程异常退出
```

原Session快照字节SHA与完整事件纯Reducer回放相等。输入52356、输出607 Token，9个Model Attempt；
8个`run_profile.agents-dump-compatible-refactor-check`调用均省略`profile`，一个仅带空selectors，其他为空对象。
前7个结果是`tool_invalid_arguments`，最后一个是`unknown/uncertain_effect`；没有Trusted Action审批，
没有可供评分的基线/最终Profile输出。本证据不把最后的unknown解释为实际容器已经执行或文件已经改写。

## 3. 源码对应、根因边界与整改方向

| 观察 | 当前源码与影响 |
|---|---|
| `profile`必填而模型持续省略 | [run_profile_schema/descriptor](../../../src/harnessix/product_config/process_action.py)已公布required与const，但说明只强调受限选择器，未显式解释工具名中的Profile不能替代参数字段 |
| 无可操作缺字段反馈 | [_normalize_invocation](../../../src/harnessix/trusted_actions/planning.py)把解码错误统一为“Action参数不符合Trusted Tool契约”，没有缺失字段提示；[只读工具](../../../src/harnessix/tools/runtime.py)已经存在安全字段反馈，可复用而不回显参数 |
| Token超限后出现保守未知 | [AgentRuntime](../../../src/harnessix/agent/runtime.py)在模型返回后、工具调度前检查用量；`_finish`对不能从原账本可靠恢复的非只读调用保守标记未知，不允许自动重放 |
| 未形成Trial报告 | [_drive_turn](../../../src/harnessix/evals/task_pack_trial.py)不将interrupted视为可直接评分终态；缺乏符合原运行合同的完成事实不能补造报告 |

这些事实支持先修正工具说明和安全参数反馈，不能通过自动补`profile`、放宽严格Decoder、跳过Profile、提高Token上限或降低评分阈值解决。
顶层没有保留原异常正文或堆栈；上表是原状态和源码能够支持的对应关系，不宣称排他确定顶层异常的完整调用栈。
产品的未知效果与模型请求费用未知是两类事实：本次所有模型费用均已知结算，不存在新增费用unknown。

## 4. 费用、独立离线与可观测性

9次请求新增已知估算0.219136元；原周期累计1.74186元，原未知预留20.77824元，分配70.00保持。
剩余复验额度39.780864元是40元减已知估算，不是增加新40元；任何新未知费用仍须立即停止。
估算不等于账单，预留不等于扣费；登记授权不追认旧unknown。

同一`940432f`并行完整离线回归：6144项中6033通过、111跳过，无失败/错误，538.252秒。
没有配置付费测试或真实集成环境变量；这是完整本机离线结果，不替代真实编码质量或Windows/Linux当前候选认证。
此前95项预算专项、1406项关联回归与6项显式Engine录制前置的范围分别见[原机制资料](../bounded-reverification-2026-09-30-v1/README.md)，各组不相加。

## 5. 证据清单、风险与后续验证

[事实](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)、[Manifest](manifest.json)
绑定配置、预注册、授权、原日志、Session有限投影和原账本SHA。公开文件不复制模型正文、工作区、数据库或机器路径。
原运行保留，不执行自动恢复或修改未知效果；离线整改后必须固定新Revision和完整Suite，并显式处置预算范围绑定。
R1～R6仍开放，R3未通过，不把6033项离线绿色宣称为生产商用发布。
