---
doc_type: source-research
status: historical
version: 1
code_revision: 459bc4de3e60bf92ed570fa99bdc39b948689591
owners:
  - core
modules:
  - evals
  - session
  - models
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
related_tests:
  - tests/evals/test_suite.py
supersedes: []
---

# 多仓库Eval Suite与Transcript证据源码研究

## 1. 研究目标与基线

本文为路线图0.9.2冻结以下问题的源码证据：主流Coding Agent如何保留可回放执行事实、如何区分模型可见
Transcript与运行时诊断、如何离线复现模型响应，以及Harnessix现有Eval为何不能直接证明多仓库基线。

| 项目 | 固定Revision | 研究范围 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | Rollout Trace、离线Reducer、E2E Benchmark入口 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | Recorded HTTP、Session事件、Project/Runner真实组合 |
| Claude Code逆向样本 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | Transcript导出、大小限制、脱敏和Session Trace |
| Harnessix | `459bc4de3e60bf92ed570fa99bdc39b948689591` | 单任务Campaign、Grader、报告与现有缺口 |

Claude Code仓库不是官方源码，只作为交叉佐证；本文不复制参考实现代码，也不把上游内部合同视为Harnessix合同。

## 2. Codex证据

### 2.1 原始观察与语义解释分离

[`rollout-trace/README.md`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/rollout-trace/README.md)
明确采用“先观察、后解释”：运行热路径写顺序化原始事件和Payload引用，离线Reducer再生成模型可见对话、Tool、
Terminal、Compaction和多Agent关系图。原始运行时输出不自动等于模型看到的正文。

[`writer.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/rollout-trace/src/writer.rs)
先落Payload文件再追加引用事件，写入后刷新Event Log；Trace失败是best-effort诊断问题，不反向破坏Agent任务。
Manifest中的Trace ID与产品Rollout ID分离，避免把诊断制品身份误作业务Session身份。

### 2.2 Transcript属于敏感本地证据

Codex明确说明Bundle可能包含Prompt、模型响应、Tool参数/结果、终端输出和路径，只在显式配置时本地生成，不是
默认Telemetry。该边界支持Harnessix继续以Session事件作为原始权威，但不支持把完整Transcript复制进低敏感度
Eval汇总报告。

### 2.3 Benchmark与产品运行时分离

[`e2e_benchmark.bzl`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/bazel/rules/e2e_benchmark.bzl)
把E2E Benchmark定义为手工运行目标，显式提供待测二进制和运行文件，并通过Workspace根包装器建立稳定路径。
可借鉴点是固定运行制品和环境；不能外推为真实任务质量、成本或人工干预已经自动度量。

## 3. OpenCode证据

[`session-runner-recorded.test.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/test/session-runner-recorded.test.ts)
使用HTTP Recorder固定模型流量，同时仍装配真实Database、Event、Session Projector、Session Store、Runner和Project，
最后同时断言模型可见消息与持久事件顺序。这证明“离线响应”不能只Mock最终文本，否则无法覆盖真实Session编排。

OpenCode的证据价值在于：

1. 固定模型输入/输出应经过真实Runner、事件和投影；
2. Project/Workspace身份属于测试前提，不应由模型输出推断；
3. Event序列与最终Transcript需要分别断言；
4. Recorded HTTP适合确定性回归，不等于真实Provider质量或计价证据。

## 4. Claude Code逆向样本证据

[`submitTranscriptShare.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/components/FeedbackSurvey/submitTranscriptShare.ts)
在显式反馈流程中规范化消息、收集Subagent Transcript，并仅在原始JSONL不超过固定上限时读取；提交前统一调用
敏感信息清洗。`sessionStorage.ts`把原始Transcript读取上限固定为50 MiB。

[`sessionTracing.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/utils/telemetry/sessionTracing.ts)
把Interaction、LLM Request、Tool、Blocked-on-user和Hook作为不同Span，并为未正常结束的Span设置TTL清理。
该证据支持单独记录等待用户和人工干预，但不支持默认上传代码正文或把Telemetry作为评分权威。

## 5. Harnessix当前事实

### 5.1 已有能力

- [`CodingEvalTask`](../../src/harnessix/evals/contracts.py)固定仓库Revision、允许修改路径、隐藏检查、预算和评分器；
- [`run_historical_coding_eval`](../../src/harnessix/evals/runner.py)通过正式Agent、Trusted Action、Session和Supervisor运行；
- [`CodingEvalCampaignReport`](../../src/harnessix/evals/campaign_contracts.py)重复同一任务，记录成功、失败分类、Token、
  Cost完整性与延迟分位；
- [`grade_coding_eval`](../../src/harnessix/evals/grader.py)从Turn投影测试、Patch、Git反馈和最终回答，不依赖唯一Golden Patch；
- Report文件采用私有权限、有界读取、临时文件`fsync`和原子替换。

### 5.2 可复现缺口

1. [`catalog.py`](../../src/harnessix/evals/catalog.py)只有一个Harnessix Bug Fix任务的三个合同版本；
2. Campaign只能重复同一`task_id/task_version`，不能聚合不同任务或仓库；
3. 现有报告没有`bug_fix/feature/refactor/test/review`分类；
4. `approvals`只统计批准次数，无法区分Eval Runner自动批准与真实人工干预；
5. Transcript只在Grader内形成临时投影，没有可绑定Run/Turn的脱敏摘要；
6. 没有Suite级任务成功率、测试通过率、人工干预率、Token、Cost和延迟统一事实；
7. 当前Historical Runner依赖POSIX且Hidden Check为单任务专用，尚不能安全执行任意外部仓库脚本。

## 6. 采用、拒绝与独立结论

| 机制 | 结论 | Harnessix取舍 |
|---|---|---|
| 原始事件与离线语义分离 | 采用 | Session Event仍是原始权威，Suite只保存结构计数和摘要 |
| Recorded模型响应 | 后续采用 | 用于确定性回归，不替代真实Provider Campaign |
| 完整Transcript写入汇总报告 | 拒绝 | 报告禁止Prompt、回答、Tool参数/输出、Diff和路径正文 |
| 单任务多试验Campaign | 保留 | 作为Case内层，不扩成另一个Runner |
| Suite跨任务聚合 | 采用 | 新增计划先行、完整证据后发布的上层合同 |
| 任意仓库自带命令直接宿主执行 | 拒绝 | 只接受版本化Task Pack和固定Container Test Profile |
| 模型裁判作为唯一正确性依据 | 拒绝 | 首版使用确定性行为、回归、Git和回答合同；人工复核单独记账 |

## 7. 对0.9.2的约束

1. Suite计划必须在首个模型请求前固定全部Case、任务版本、仓库Revision、Campaign指纹和环境；
2. 合同下限覆盖五类任务和至少两个仓库；0.9.2关闭证据采用不少于10个Case、三个仓库且每类不少于两个；
3. Suite不得发布部分成功，崩溃恢复只能从完整Campaign/Session事实重建；
4. 自动Eval审批不计人工干预；人工Actor审批、提问、Steering和`manual_intervention`均计入；
5. 成本未知必须显式标记并停止后续收费试验，不能按零处理；
6. 原始Session/Artifact按高敏感度保留，Suite Report只能携带摘要、计数、固定身份和低敏感度失败分类；
7. 真实多仓库任务不得在宿主直接执行仓库脚本，必须经过固定镜像、资源、网络和输出边界。

## 8. Go/No-Go

结论：**Go，但必须分片实施。** 现有Campaign、Session和成本合同足以复用；直接向现有单任务Catalog堆入五类任务会
继续放大硬编码检查与POSIX宿主执行风险，因此先建立Suite/Transcript正式合同，再建设Task Pack、可恢复Suite Runner、
多仓库任务集和真实基线。详细实施由[ADR 0082](../adr/0082-multi-repository-eval-suite-and-transcript-evidence.md)和
[0.9.2详细设计](../changes/m09-2-eval-suite-and-transcript-baseline.md)约束。
