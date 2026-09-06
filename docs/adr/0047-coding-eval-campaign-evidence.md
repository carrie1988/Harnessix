# ADR 0047：Coding Eval多试验计划与可重算证据

- 状态：已接受并实现0.5.5c1
- 日期：2026-09-06
- 范围：Campaign计划、单次归类、Token/时延/成本聚合与原子报告

## 1. 背景

0.5.5b2证明同一正式Runtime能够完成一个真实历史缺陷，但确定性脚本Provider不能回答真实模型的成功率、失败原因、成本和时延。直接执行若干真实请求后手工抄表也不足以形成生产证据：运行参数可能漂移，失败调用可能被漏记，未知Usage可能被当成零，不同价格或计费上下文可能混入同一总数。

[源码研究](../research/eval-campaign.md)表明主流Coding Agent都把终态、Usage和成本与Turn或步骤关联。Harnessix已有持久ModelAttempt和可重算CostReport，因此本片复用这些事实，不增加第二套模型计费或会话账本。

## 2. 决策

0.5.5c拆分为：

1. **c1（本片）**：离线完成Campaign计划、证据核对、失败分类、聚合和原子报告；
2. **c2**：c2a增加默认禁网的受控执行入口，先发布计划，再顺序运行固定Provider试验；c2b在已授权的3次试验和人民币10元停止线内生成真实基线记录，见ADR 0048。

`CodingEvalCampaignPlan v1`在请求前固定Campaign/任务身份、Harnessix revision、Provider、精确模型、2—20个有序唯一运行ID、价格快照和计费上下文。模型必须与价格快照一致；平台、地域、服务等级、推理模式和缓存TTL必须逐项匹配；创建时间必须位于价格有效期。完整计划生成内容指纹。

每个运行ID仍使用0.5.5b2独立目录。Campaign没有共享Thread、工作区、Patch或Action，因而每个试验从相同历史缺陷和空模型上下文开始。Campaign计划与最终报告分别以0600临时文件、文件/目录`fsync`和同目录原子替换发布；读取拒绝权限放宽、符号链接、损坏和超限内容。

## 3. 证据核对与分类

`CompletedCodingEvalTrial`只是在进程内组合四项既有事实：completed运行状态、Eval报告、原Turn和CostReport。构建器逐项核对：

- run/task/version/fingerprint/environment、Thread/Turn和报告摘要；
- 报告起始时间、基线观察、模型步骤及输入/输出Token；
- CostReport的Turn身份及逐尝试快照；
- 每个价格绑定与Campaign固定价格、计费上下文完全一致；
- CostReport能够从原Turn和绑定重新构造且逐字段相同。

不完整Campaign、交叉运行证据、缺失价格绑定、价格漂移、币种混用和不可重算报告均fail closed，不发布部分成功。

主失败分类按以下优先级确定：

1. Eval通过：`passed`；
2. 数据集/基线无效：`eval_infrastructure`；
3. Turn的规范失败类别为Provider：`provider`；
4. Turn失败类别或Eval检查包含预算失败：`budget`；
5. Turn存在其他非Provider失败：`runtime`；
6. 正确性、回归、越界修改或最终回答失败：`task`；
7. 其他未完成Turn：`runtime`。

Provider分类只保存`ResponseFailed`固定错误码和retryable；未知错误映射为`unknown`。Agent错误消息、模型正文、Prompt、Diff、测试输出、response ID和凭据不进入Campaign报告。

## 4. 聚合语义

报告保存每次运行的报告摘要、Turn ID/终态、主分类、Eval失败集合、实际模型集合、尝试数、Token、墙钟时延和成本完整性。汇总记录六类数量、总尝试、总Token、时延min/P50/P95/max、已知成本小计及成本不完整运行ID。

P50/P95使用nearest-rank，不插值。`complete`要求每个试验的每次模型尝试都有完整Usage和有效价格；存在已知小计但仍有未知项时为`partial`；完全没有可计价事实时为`unknown`。未知成本从不填零，已知小计不称为实际账单。

## 5. 失败与恢复

c1不发起模型请求。计划文件是c2恢复的前置锚点：c2必须先持久化计划，再按固定run ID调用0.5.5b2运行器。若宿主在单次试验后退出，重开同一run ID由Session/Action/Patch账本恢复；若单次报告已经完成，运行器只读取原报告，不重发模型请求。只有计划中的全部运行均完成并通过交叉核对后，Campaign报告才能发布。

本片自身没有Campaign执行状态、费用停止策略或CLI；这些边界已由后续c2a和ADR 0048实现。真实基线仍必须通过正式入口生成，不得用手工循环替代。

## 6. 安全与限制

- 计划不保存API Key值或环境变量内容；本片自身不读取任何凭据环境变量；
- 价格快照是估算依据，不是账单；计价适用性必须由宿主核对；
- 真实费用、模型和试验次数必须逐Campaign显式授权；首轮授权不自动适用于后续基线；
- 无OS Sandbox，仍禁止任意第三方仓库、动态测试命令和网络开放；
- c1完成不代表0.5.5c或真实模型基线完成。
