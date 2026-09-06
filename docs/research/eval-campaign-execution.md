# Coding Eval真实Campaign执行与费用边界研究

- 日期：2026-09-06
- 范围：默认网络策略、重试、费用停止、持久恢复和凭据边界
- 固定源码：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`、Claude Code源码样本 `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`

## 1. 源码事实

### 1.1 Codex：网络是显式权限，不是模型调用的隐式副作用

`codex-rs/app-server/src/thread_state.rs:269-273`创建线程状态时采用只读Sandbox，且`network_access: false`。协议层另行传递和核对权限配置，而不是由工具或模型自行打开网络。

结论：真实Eval入口默认必须禁网；未提供显式开关时不得读取运行配置或凭据，更不能因配置文件存在而建立连接。

### 1.2 OpenCode：重试是有状态策略，不应藏在基准里

`packages/opencode/src/session/retry.ts`显式定义可重试错误、响应头等待、指数退避、抖动和最多重试次数；`session/processor.ts`在循环中决定是否重试。重试会改变请求次数、费用、时延和失败分类。

结论：首个可比较基线关闭Provider自动重试，每个模型请求最多一次尝试。未来如评估重试策略，必须作为单独Campaign参数和报告维度，不能在同一基线中隐式启用。

### 1.3 Claude Code源码样本：费用是终态的一部分，预算超限是独立失败

`src/entrypoints/sdk/coreSchemas.ts:1417-1442`在SDK结果中暴露`total_cost_usd`，并把`error_max_budget_usd`列为独立结果类别；`src/QueryEngine.ts:983-990`在预算超限结果中同时返回累计费用。

结论：费用停止原因必须持久化并与已完成试验证据绑定，不能只打印临时告警。停止后重开只能读取同一事实，不能继续请求。

## 2. 百炼北京接入事实

阿里云百炼北京地域支持OpenAI兼容入口`https://dashscope.aliyuncs.com/compatible-mode/v1`，API Key具有地域范围。生产租户隔离场景宜使用专属Workspace端点；当前单用户基线未提供Workspace ID，因此使用共享北京兼容端点。端点与地域说明以[百炼北京接入说明](https://help.aliyun.com/zh/model-studio/beijing-access-information)为准。

首轮真实基线固定精确快照模型`qwen3-coder-plus-2025-09-23`、非思考模式和串行工具调用。价格快照采用北京按量付费、输入不超过32K区间的输入每百万Token 4元、输出每百万Token 16元；价格和模型能力以执行前核对的[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)与[Qwen Coder说明](https://help.aliyun.com/zh/model-studio/qwen-coder)为准。价格快照只用于本地估算，不替代供应商账单。

## 3. Harnessix决策

1. 新增独立`CodingEvalCampaignRunConfig v1`，固定计划、源码/运行根、Git/Python程序、Provider配置和费用停止线；配置只保存API Key环境变量名，不保存值。
2. CLI必须同时给出`--config`和`--allow-network`才读取配置。缺少网络开关时只输出白名单`network_not_enabled`，退出前不触碰文件、Provider或环境Key。
3. 配置文件必须是0600普通文件，拒绝符号链接、FIFO、目录、空文件、超过512 KiB、非法UTF-8、重复JSON键及NaN/Infinity。
4. Campaign先以0600原子文件发布固定计划，再创建Provider。根目录和`runs/`必须是0700；单Campaign使用0600非阻塞文件锁，防止两个宿主并发消费同一run前缀。
5. 试验严格按计划run ID顺序执行，每个run使用独立的0.5.5b2运行目录。Provider上下文可以复用连接池，但不共享Thread、Session、工作区或模型上下文。
6. Provider配置强制`max_attempts=1`、`retry_delay_seconds=0`、支持工具调用且禁用并行工具调用。Agent在一个试验内为完成任务产生多个模型步骤属于正常Agent Loop，不等于Provider重试。
7. 每个试验完整终结后，从原Turn和固定价格绑定重算成本；成本不完整则持久停止为`cost_unknown`。已知累计成本达到停止线时，在创建下一个试验请求前停止为`fee_limit_reached`。
8. 完成全部试验后才构造Campaign报告；报告发布与状态终结分为两个原子文件写入点，重开会核对报告正文、完成前缀和摘要后补齐终态。
9. CLI只输出原因、Campaign ID、计划/完成计数、报告发布标志和已知金额，不输出路径、配置、模型正文、供应商错误、响应ID或凭据。

## 4. 费用停止线的精确定义

费用停止线是**Harnessix在试验边界上的本地停止策略**，不是百炼账户硬额度：

- 当前Provider在响应结束后才返回Usage，无法在单个请求流中精确预知最终费用；
- 一个已开始试验可能包含多个Agent模型步骤，也可能使累计金额超过停止线；
- 达线后保证不启动下一试验，但不承诺撤销已产生费用；
- Provider超时、断流或Usage缺失时，已知小计仍保留，但Campaign以`cost_unknown`停止；
- 供应商控制台预算、欠费保护和账单对账不在本执行器的控制面内。

## 5. 恢复与失败边界

执行状态只承认计划run ID的有序完成前缀。每次打开都重新读取单次运行状态、报告和Session Turn，重算成本并核对状态金额；调用方不能通过修改汇总金额跳过费用门禁。

已覆盖的关键窗口包括：计划发布后Provider创建前退出、单次报告完成后Campaign状态提交前退出、Campaign报告发布后终态提交前退出、并发锁冲突、源码revision漂移、状态/报告损坏及已停止Campaign重开。恢复只读取或继续固定run ID，不生成替代ID。

当前执行仍在宿主权限下运行固定历史任务，没有OS Sandbox、任意仓库接入、供应商账单对账或实时请求级硬费用上限。这些限制必须与真实基线结果同时披露。

## 6. 首轮实测反馈

本设计在提交`bbfd446`上完成百炼北京三次真实运行。网络、串行Function Calling、完整Usage、精确模型身份、无自动重试、聚合和费用门禁均按设计工作；三次均因任务v1的20000累计Token预算在第五个模型步骤终结，尚未执行目标实现读取和修改。

这一结果将新的研究重点从“能否安全执行真实Campaign”转为“如何让版本化任务预算反映真实工具Schema、历史消息和工具结果的累计输入开销”。原Campaign保持不可变；预算调整和重基线划入0.5.5c3，详见[验证记录](../validation/bailian-2026-09-06-coding-eval/README.md)。
