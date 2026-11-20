---
doc_type: adr
status: deprecated
version: 2
code_revision: f100487eb7af81b7366b26a0990db6685f3091ef
owners: [core, security]
modules: [secrets, product_config, context, agent]
related_adrs:
  - docs/adr/0132-append-only-requests-and-token-calibrated-compaction.md
  - docs/adr/0167-proactive-summary-request-reuses-the-main-request-prefix.md
  - docs/adr/0168-host-overhead-per-model-step.md
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
related_tests: []
supersedes: []
---

# ADR 0169 出站请求的扫描容量加倍与四分之三窗口压缩点（未采纳）

- 状态：未采纳（A/B未达到改为默认的条件，代码没有合入；YAML状态`deprecated`：保留背景，新实现不得以此为依据）
- 日期：2026-11-17提议，2026-11-20按A/B结果定为未采纳
- 修订：无。产品代码、配置字段、Schema与默认值都没有变化

## 背景

产品对每个发出的模型请求做公开输出保护扫描（`SecretPublicationScope.assert_public_request`），确认请求里没有受保护的
Secret。扫描与其余公开输出共用同一组上限：1 MiB字节、64 MiB工作量。为了让请求总能通过扫描，Context预算把单次请求
的输入限制在1 MiB的四分之三（786,432字节，`context_budget._REQUEST_INPUT_CAP`），压缩触发点还要在输入上限之下留出
一次退避的余量（ADR 0132）。

2026-11-15/16 NL2RepoBench正式回归（0.10.0b48，窗口262,144 Token）实测约每Token 4.2字节：单次请求最多约18.7万Token，
压缩触发点最高约13.8万，产品默认在窗口一半（约13.1万）压缩。Harnessix的请求最大14万Token、压缩57次；mini-swe-agent
不压缩，请求一直用到26.2万Token，平均得分不低于Harnessix。在这个上限下推迟压缩点没有意义：设成窗口的四分之三，触发点
最多从13.1万推到13.8万。

## 第1版提议（2026-11-17，负责人批准先做A/B）

1. 只给出站模型请求加倍扫描预算（2 MiB、128 MiB工作量）；其余公开输出仍是1 MiB与64 MiB。
2. 产品配置`compaction`新增`three_quarter_window`：在换算窗口的四分之三处触发、压到四分之一；只有这个压缩点使用
   加倍后的请求输入上限。`half_window`与`third_window`的数值不变。
3. 默认值不变，先在NL2RepoBench 39题上A/B。改为全部模型的默认值需要同时满足：得分相对对照组不劣（差距不超过
   一个标准误），且耗时至少下降5%（负责人2026-11-18决定）。

如果改为默认，云端100万窗口的模型会从约14万Token推迟到约29万Token才压缩，每次请求的输入与按Token计费的费用随之
增加；这一点原计划在A/B之后单独请负责人决定。

## A/B结果（第2版，2026-11-20）

- 条件：qwen3.8-flash-next（247本地vLLM，窗口262,144 Token）；NL2RepoBench 39题（Harbor）；每题2小时、300步；得分为
  每题官方测试通过比例；2026-11-18至20四组同批运行，共8并发。
- 实验组：四分之三窗口，两轮（O3a、O3b），实验分支`feat/o3-request-scan-and-late-compaction`（未合入、未公开）。
- 对照组：同一批的候选版本（提速轮，默认`half_window`），一轮。逐题配对，Bootstrap求标准误。

| 比较 | 得分差（标准误） | 耗时差（标准误） |
|---|---|---|
| 实验组两轮 vs 对照组（主比较） | +0.0305（0.0444） | −0.16分钟/题，−0.2%（2.86） |
| 去掉O3a中因整机内存耗尽被连带失败的box | +0.0379（0.0452） | +1.0%（2.69） |
| 实验组 vs 候选版本两批 | +0.0233（0.0369） | +3.1%（2.32） |

得分不劣，耗时没有下降，不满足“至少下降5%”。NL2RepoBench约一半的题用满2小时预算，这部分题的耗时被预算封顶；
只看各组都在预算内完成的12题，耗时差为+2.3%（标准误6.77），同样没有下降。

## 决策

**不采纳。** 两项都不合入，产品默认仍是`half_window`：

1. **不加倍出站请求的扫描预算。** 它只为四分之三窗口服务；没有这个压缩点就没有收益，却放宽了一处公开输出保护的上限，
   并让每步的扫描时间随请求变长而增加。
2. **不新增`three_quarter_window`可选值。** 没有测得收益，新增配置面需要长期维护；用户在云端大窗口模型上打开它，
   会推迟压缩、增加按Token计费的费用。

重新提议需要新的证据：在耗时不被预算封顶的任务或指标上，得分不劣且耗时至少下降5%；或者出现让长请求明显更便宜的
服务端条件（例如前缀缓存命中率显著提高）。届时按新的编号提出，本记录保留实验条件与数据。

## 后果

- 代码与文档的当前事实不变：请求输入上限仍是1 MiB扫描容量的四分之三，压缩点仍是`half_window`与`third_window`。
- 实验分支只保留在本地，不进入发布；本记录是这次A/B的唯一正式留档。
