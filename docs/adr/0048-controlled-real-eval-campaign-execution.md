# ADR 0048：受控真实Coding Eval Campaign执行

- 状态：已接受；0.5.5c2a执行基础设施与c2b首轮三次真实基线均已完成
- 日期：2026-09-06
- 范围：显式网络准入、固定执行配置、费用停止、持久进度和恢复

## 1. 背景

ADR 0047已经能对一组完成试验重算质量、Token、时延和成本，但不负责发起请求。直接用临时脚本循环真实Provider存在四个不可接受的问题：计划可能在首个请求后才补写；SDK重试会隐式扩大样本和费用；宿主退出后无法确定下一run；费用未知或已达线时仍可能继续发送。

[执行与费用研究](../research/eval-campaign-execution.md)表明网络权限、重试策略和费用终态都应成为显式、可恢复的控制面。0.5.5c2因此分为：

1. **c2a（本片）**：实现默认禁网CLI、正式配置/状态/结果契约、锁、试验间费用停止与恢复；
2. **c2b**：使用已授权的精确模型、三次独立试验和人民币10元停止线生成并归档真实基线。

## 2. 契约与不变量

新增三份冻结v1契约：

- `CodingEvalCampaignRunConfig`：内嵌不可变Campaign计划，绑定规范源码根、私有运行根、Git/Python绝对路径、`OpenAIChatConfig`和同币种正数费用停止线；
- `CodingEvalCampaignExecutionState`：保存计划/配置指纹、有序完成run前缀、已知成本、`ready|running|stopped|completed`、停止原因和最终报告摘要；
- `CodingEvalCampaignRunReport`：CLI白名单结果，只暴露规范原因、计数、发布状态和已知金额。

配置强制精确模型等于计划模型、工具调用开启、并行工具关闭、Provider最多一次尝试且重试延迟为零。运行根不得等于源码根或位于源码根之下。执行前再次核对内置任务版本/指纹、平台、隔离声明和当前Git HEAD；不匹配时在Provider创建前失败。

执行状态只允许计划run ID的有序完成前缀。状态里的已知金额不是权威输入：每次重开均从各run的completed状态、Eval报告和Session Turn绑定相同价格快照重算，并要求结果精确相等。

## 3. 执行顺序

```text
显式--allow-network
  → 安全读取0600配置
  → 创建/核对0700私有根和0600非阻塞锁
  → 发布/核对campaign-plan.json
  → 创建/读取campaign-state.json并重算完成前缀
  → 核对任务、平台、源码revision和宿主程序
  → 打开单一Provider上下文
  → 按固定run ID调用0.5.5b2正式运行器
  → 每次结束重算并持久化成本
  → 未知成本或达线时停止，否则执行下一试验
  → 全部完成后发布campaign-report.json
  → 记录报告SHA-256并标记completed
```

复用一个Provider上下文只用于SDK连接生命周期；每次试验仍拥有独立物化、Thread、Session、Action/Patch账本和模型上下文。一个试验中的多个模型步骤是Agent Loop，不是Provider重试。

## 4. 费用语义

费用停止线只在完整试验之间执行。当前Usage来自响应终态，故单个已开始试验可能使累计已知金额越线；达到或超过阈值后不得启动下一试验。若任一已完成试验的CostReport不是`complete`，状态持久停止为`cost_unknown`，即使仍存在已知小计也不继续。

该停止线不是供应商账户硬预算，不撤销已发生费用，也不替代账单。首次真实基线的授权边界为三个独立试验、人民币10元累计已知费用停止线、Provider不自动重试。

## 5. 持久化与恢复

计划、状态和最终报告均使用既有0600临时文件、文件/目录`fsync`和同目录原子替换。Campaign根及`runs/`要求0700；锁文件要求0600并使用`flock(LOCK_EX|LOCK_NB)`。

恢复规则：

- 已发布计划必须与配置完整相等；
- 已有状态必须匹配Campaign、计划和配置指纹；
- 单次运行在报告已发布但Campaign尚未提交run前缀时，以同一run ID进入0.5.5b2只读重开，不发起替代试验；
- Campaign报告已发布但状态未完成时，重算全部试验并比较完整报告后补写completed；
- stopped重开只核对原因和证据，不再打开Provider；
- completed却缺少报告、报告早于全部试验、摘要/金额/顺序漂移均fail closed。

## 6. CLI与凭据

`harnessix coding-eval-campaign --config <path>`默认返回`network_not_enabled`，且不读取配置。只有额外提供`--allow-network`才读取文件。配置拒绝链接、非普通文件、非0600权限、空/超限内容、非法UTF-8、重复键和非有限JSON数值。

配置只记录`api_key_env`；值由启动宿主的私有环境注入，不写入仓库、配置、状态、报告或命令行。CLI禁用应用日志并只打印`CodingEvalCampaignRunReport`，解析错误不回显参数或配置正文。

## 7. 测试与限制

离线测试使用正式0.5.5b2运行器和可计价确定性Provider，覆盖两次独立试验、费用/未知成本停止、锁与revision门禁、计划先于Provider、状态/报告崩溃恢复、配置文件边界、Schema和白名单输出。默认CI仍不依赖网络或API Key。

c2a完成不代表真实模型质量已经有效测量。当前没有请求内实时费用中止、供应商账单对账、OS Sandbox、任意第三方仓库或通用生产多租户密钥托管；这些能力不得从本ADR推断。

## 8. c2b真实基线结果

提交`bbfd446`经四项CI通过后，百炼北京`qwen3-coder-plus-2025-09-23`按本ADR执行三个独立run。Campaign完整发布，15个模型步骤均只有index 1尝试，Usage和模型身份完整，已知成本¥0.273748，未触发10元停止线，也没有Provider或Eval基础设施失败。

三次Turn均在第五步以`budget_exceeded`终结。真实累计输入为20999—21098 Token，已超过任务v1的20000总Token预算；当时模型刚请求读取目标`_chat_stream.py`，工具尚未执行，三个工作区都没有修改。故0/3结果证明的是任务预算不适配，不能归因于模型编码能力。

脱敏计划、逐run指标、报告和根因见[真实基线记录](../validation/bailian-2026-09-06-coding-eval/README.md)。后续0.5.5c3必须版本化调整任务预算并重新授权Campaign；不能修改本次计划或把追加run并入既有报告。
