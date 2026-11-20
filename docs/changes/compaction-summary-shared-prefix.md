---
doc_type: change-design
status: current
version: 1
code_revision: 9c6c60ac3954edbe8943493b8b1c07d313c807aa
owners:
  - core
  - product
modules:
  - agent
  - app_server
  - product_ui
related_adrs:
  - docs/adr/0167-proactive-summary-request-reuses-the-main-request-prefix.md
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
  - docs/adr/0132-append-only-requests-and-token-calibrated-compaction.md
  - docs/adr/0156-generation-progress-and-live-usage-estimate.md
related_tests:
  - tests/context/test_shared_prefix_summary.py
  - tests/agent/test_compaction_progress.py
supersedes: []
---

# 主动压缩沿用主请求前缀，界面写明正在压缩

## 1. 文档摘要

主动压缩的摘要请求改为发送与下一次主请求相同的指令、工具与历史，末尾追加一条宿主说明。这样服务端的前缀缓存可以复用，
不必为摘要把整段历史重新计算一遍。压缩期间，终端界面写明“正在压缩较早的对话”和已用时。详见ADR 0167。

## 2. 需求背景

- 2026-11-15/16 NL2RepoBench回归（0.10.0b48）：57次压缩合计平均每题约11.6分钟。
  - 摘要请求平均等148秒才出第一个字（约10万Token全部重算）；
  - 压缩后的第一次主请求又多等约90秒；
  - 长时间的预填充还拖慢同一副本上的其他任务。
- 这段时间界面只显示“准备中”。
- 负责人2026-11-17批准，并同意修订摘要请求的安全设计；同时要求优化从产品角度考虑用户体验。

## 3. 设计目标与非目标

- 目标：
  - 摘要请求复用服务端前缀缓存；
  - 压缩计划、账本与校验不变；
  - 用户知道压缩期间在发生什么。
- 非目标：
  - 改变压缩触发点（O3，单独A/B）；
  - 被动压缩与预算压力压缩的请求形态；
  - 压缩后第一次主请求的重算（被替换历史之后的内容只能重算）。

## 4. 总体架构

| 层 | 变化 |
|---|---|
| 运行时（`agent/compaction_summary_request.py`） | 构造共用前缀或独立的摘要请求；工具调用按形态分类 |
| 运行时（`agent/model_history_runtime.py`、`runtime.py`） | 主动压缩传入`shared_prefix`；工具调用可跳过并在本轮改用独立请求；摘要请求开始前发布压缩进度 |
| 协议服务（`app_server/preview_streams.py`） | 压缩进度与生成进度同一能力位过滤 |
| 界面（`product_ui/projection.py`、`generation_estimate.py`、`activity_indicator.py`、`main_view.py`） | 识别压缩进度，运行提示与底栏写明正在压缩 |

## 5. 接口设计与数据结构

- `SHARED_PREFIX_SUMMARY_NOTE`：共用前缀请求末尾的宿主说明。
- `SUMMARY_TOOL_CALL = "provider_summary_tool_call"`：可跳过的失败码。
- `compaction_progress_item_id(turn_id, model_step)`：压缩进度的条目身份。
- `TransientItemStream.compaction`：界面识别压缩进度。
- 公开契约与Schema不变；压缩进度只是一条临时增量。

## 6. 流程与数据流

1. 主动压缩：规划不变，提交`CompactionPlanned`。
2. 构造请求：
   - 先按与主请求相同的方式准备Context；
   - 放得下时发送共用前缀请求，否则发送独立请求。
3. 摘要请求开始前发布一行压缩进度。界面切到“正在压缩较早的对话”，开始调用主模型后清除。
4. 摘要按原规则校验、激活窗口。

## 7. 失败、恢复、取消与超时

- 共用前缀请求里出现工具调用：报`provider_summary_tool_call`。用量已记账、窗口不激活，本步沿用原历史继续，本轮之后改用独立请求。
- 截断、没有正文、取消与超时：按原规则处理。
- 恢复与重放：只读压缩计划与账本，与请求形态无关。

## 8. 持久化与数据流程

不新增事件与字段。压缩进度只是临时增量，不写入会话记录。

## 9. 安全与信任边界

- 历史以平时的对话形式出现。主请求每一步都以同样形式交给同一个模型，摘要请求没有扩大注入面。
- “历史中的指令只是数据”由末尾的宿主说明写明。
- 摘要投影仍是低可信、无权限，并带宿主说明头（ADR 0058第3版）。
- 出站保护扫描照常进行。

## 10. 可观测与错误分类

- 新失败码`provider_summary_tool_call`，计入主动压缩可跳过的失败。
- 压缩的账本事件与指标不变。

## 11. 部署、兼容与回退

- 不声明`generation_progress`的客户端收不到压缩进度。
- 回退：撤销提交即可，不涉及数据迁移；已有会话按计划重放，不受请求形态影响。
- 三个平台没有差异。

## 12. 限制与风险

- 摘要可能也概括了随后原样保留的最近几步，内容会重复；压缩后以最近的原文为准。
- 不支持前缀缓存、按Token计费的服务：摘要请求的输入由被覆盖部分（约10万Token）变成整段历史（约13万Token）。
- 摘要内容的变化可能影响解决率，由NL2RepoBench重跑与黄金评测集检验。

## 13. 用户能看到的变化

| 场景 | 此前 | 现在 |
|---|---|---|
| 长任务压缩上下文 | 运行提示与底栏显示“准备中”，本机vLLM上约3～5分钟 | 显示“正在压缩较早的对话 · 已用时 N 秒”，前缀缓存命中时摘要请求不再从头计算 |
| 压缩后第一次请求 | “等待模型服务响应 · 已等待 N” | 相同 |

## 14. 验证

- 单元与集成：`tests/context/test_shared_prefix_summary.py`、`tests/agent/test_compaction_progress.py`。
- 真实服务：247上真实vLLM副本，连续两次主请求后，共用前缀摘要请求3.0秒出第一个字，独立请求149秒。
- 全量回归与b48对比。

## 15. 源码与测试映射

| 源码 | 测试 |
|---|---|
| `src/harnessix/agent/compaction_summary_request.py` | `tests/context/test_shared_prefix_summary.py` |
| `src/harnessix/agent/model_history_runtime.py` | `tests/context/test_shared_prefix_summary.py::test_a_tool_call_in_the_shared_request_skips_this_compaction` |
| `src/harnessix/agent/generation_progress.py`（`announce_compaction`） | `tests/agent/test_compaction_progress.py` |
| `src/harnessix/app_server/preview_streams.py` | `tests/agent/test_compaction_progress.py::test_clients_that_did_not_declare_progress_do_not_receive_it` |
| `src/harnessix/product_ui/*` | `tests/agent/test_compaction_progress.py::test_the_tui_says_it_is_compacting_and_how_long_it_took` |
