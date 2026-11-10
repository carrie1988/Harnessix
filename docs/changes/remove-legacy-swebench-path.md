---
doc_type: change-design
status: current
version: 1
code_revision: 4992c2e7937ef7da35fad6468e0acee7940a9cbc
owners:
  - core
modules:
  - evals
  - product_config
  - product_ui
  - candidates
related_adrs:
  - docs/adr/0120-system-neutral-evaluation-platform.md
  - docs/adr/0076-code-readability-and-structural-governance.md
related_tests:
  - tests/evals/test_legacy_swebench_removed.py
  - tests/candidates/test_selection.py
  - tests/product_config/test_coding_tool_view.py
  - tests/product_config/test_coding_tool_view_runtime.py
  - tests/product_config/test_compact_workspace_patch.py
  - tests/governance/test_readability_policy.py
supersedes: []
---

# 删除旧本地SWE-bench评测路径

## 1. 文档摘要

本次删除产品包内只为旧本地SWE-bench评测服务的代码：`harnessix coding-eval-swebench`命令、
`harnessix.evals.swebench`包、只由它开启的复现提示与复现门禁、候选择优中针对Django/SymPy的测试入口规则，
以及仓库内已退役的运行脚本。删除前最后版本是`4992c2e7`，完整历史在Git中。

## 2. 需求背景

- 2026-11-08用户决定停用一期本地运行器，此后评测一律走Harbor与Langfuse
  （[ADR 0120](../adr/0120-system-neutral-evaluation-platform.md)）。
- 独立审计第12项指出：产品wheel打包整个`src/harnessix`，Django/SymPy专用逻辑仍随包发布。App Server不导入
  `harnessix.evals`，但`harnessix coding-eval-swebench`和退役脚本仍可触达这些代码。
- 2026-11-10用户决定12A：删除。

## 3. 设计目标与非目标

目标：产品wheel不再包含SWE-bench、Django或SymPy专用代码；默认产品行为逐字不变；保留仍有通用价值的代码。

非目标：不改动`integrations/`、`packages/evalkit/`和`benchmarks/swebench/`下的冻结证据；不清理其他只由旧路径开启、
但不含基准专用内容的视图与提示开关（见第12节）。

## 4. 总体架构：删除与保留

| 删除 | 原因 |
|---|---|
| `scripts/swebench_local_run.py`及驱动它的`run_week1_holdout.sh`、`run_phase2_continuation.sh` | 运行器已退役；模型名与语言预设已在`4992c2e7`迁到`integrations/harbor/task_environment.py`，无其他导入方 |
| 顶层CLI的`coding-eval-swebench`分派与帮助项 | 旧路径入口 |
| `src/harnessix/evals/swebench/`（27个模块）与`tests/evals/swebench/`（37个文件） | 预注册、公开测试Profile（含Django `tests/runtests.py`、SymPy `bin/test`处理）、v2～v40运行配置、执行、prediction、官方评分导入与工作区退役，仅本命令和退役脚本使用 |
| `product_config/coding_reproduction_gate.py`、`agent_context`的`reproduction_guidance`与复现提示（含“for Django”） | 只由旧运行配置开启 |
| `CodingWorkflowToolView`的`scratch_reproduction`、`require_failing_reproduction`、`admit_call`及草稿投影；`SCRATCH_EXCLUDE_PATHSPECS` | 同上；产品与Task Pack始终使用默认关闭 |
| `candidates/selection.py`中的`runtests`、`bin/test`模式 | 只服务旧A2x4编排 |

| 保留 | 原因 |
|---|---|
| `harnessix.candidates`选择层 | 通用的多候选验证择优，暂无调用方，见[模块设计](../modules/candidates.md) |
| `product_config/coding_scratch.is_scratch_path` | 候选交叉检查仍用它叠加另一候选的草稿文件 |
| Agent Runtime的`ModelToolCallAdmission`端口 | 通用准入机制，另有独立测试 |
| `scripts/swebench_local_report.py`等报告与镜像脚本、`benchmarks/swebench/` | 不进入wheel，属于历史证据或通用工具；`benchmarks/swebench/`下冻结的宿主脚本仍导入已删除的包，按冻结规则不改写，只能在`4992c2e7`及更早版本上运行 |

## 5. 接口设计与数据结构

- `harnessix coding-eval-swebench ...`现在按未知命令被argparse拒绝，退出码2。
- `build_product_agent_context`去掉`reproduction_guidance`参数；`CodingWorkflowToolView`去掉`scratch_reproduction`、
  `require_failing_reproduction`字段及`admit_call`方法。传入这些参数会得到`TypeError`。
- `is_discriminating`只识别通用测试入口；`python tests/runtests.py ...`仍计入，裸`./tests/runtests.py`与`bin/test`不再计入。
- 公共`__all__`只少了`harnessix.evals.swebench`一项；Schema与`spec/`无变化。

## 6. 流程与数据流

产品服务端与Task Pack试次本来就以默认参数构造视图和Context，调用顺序不变。外部基准评测的流程不经过产品包：
Harbor集成通过公共Agent Protocol启动`harnessix exec`，评分由[官方评分封装](../../integrations/swebench/official_grader.py)
和[evalkit](../../packages/evalkit/src/evalkit/)完成。

## 7. 失败、恢复、取消与超时

默认路径的失败、恢复、取消与超时语义不变。复现门禁删除后，视图不再产生`workspace_patch_reproduction_required`；
历史Session中已持久化的该拒绝结果仍按原结果重放，不需要门禁代码。

## 8. 持久化与数据流程

不改变任何持久化格式。旧运行器写下的运行目录、prediction与报告仍是普通文件，按需用`4992c2e7`版本的代码读取。

## 9. 安全与信任边界

删除只缩小wheel可触达的代码面，不放宽任何审批、沙箱或网络边界。外部基准的隔离与隐藏测试仍只在Harbor与官方Evaluator侧。

## 10. 可观测与错误分类

不新增错误码。移除的`workspace_patch_reproduction_required`只由已删除的门禁产生。

## 11. 部署、兼容与回退

- 只影响显式使用`coding-eval-swebench`或上述开关的调用方，产品默认配置不受影响。
- 回退：按提交逐个`git revert`即可恢复，无数据迁移。

### 可读性预算说明（ADR 0076）

删除只减少结构债务，策略按本次实际变化收紧，没有重新生成整份策略：

- 去掉`harnessix.evals.swebench`的公共`__all__`快照、该包的2个超大文件和11个超大符号预算；
- 去掉只由该包产生的依赖边`evals->delivery`、`evals->trusted_actions`；
- `coding_tool_view.py`文件上限1016→895；`CodingWorkflowToolView`、其`__post_init__`与`select`、
  `build_product_agent_context`的行数与复杂度上限降到删除后的实际值；`_latest_review_is_clean`降到阈值以下，移出预算。

最终报告`docs/baselines/readability-0.9.0-final.json`只施加同一差异。基线`4992c2e7`上已有的漂移原样保留，
不属于本次改动：`AgentRuntime._sample_events`复杂度86>85、`CodingToolRuntime`302>296行，以及相应的
汇总计数（函数与有说明函数各+3、物理行+63、逻辑行+47）。

## 12. 限制、风险与后续差距

- `CodingWorkflowToolView`与`build_product_agent_context`仍有一批只由旧运行配置开启、产品默认关闭的开关
  （紧凑指令、收敛指引、自我校验、同类实现检查、修复循环上限、截止Patch等）。它们不含基准专用内容，本次不删除，
  是否清理需另行决定。自我校验说明中“extend the scratch test”的措辞也随之保留。
- `harnessix.candidates`暂无调用方，接入产品前需重新设计编排层。
- [历史变更设计](swebench-adapter-verified-25-pilot.md)已标为historical，指向已删除源码的链接改为`4992c2e7`永久链接。

## 13. 验证

- 新增[`test_legacy_swebench_removed.py`](../../tests/evals/test_legacy_swebench_removed.py)：CLI帮助不列出、调用被拒；
  产品包内没有`swebench`模块、复现门禁和`runtests`/`bin/test`规则；视图与Context不再接受被删参数。
- `tests/evals`、`tests/product_config`、`tests/candidates`、`tests/integrations`、`tests/context`、CLI冒烟测试及
  可读性策略测试：本分支的失败集合是基线`4992c2e7`失败集合的子集，没有新增失败。剩余失败均为本机环境问题
  （Apple git 2.24不支持`--object-format`、沙箱禁止本地Socket、本机`PYTHONPATH`含仓库根导致Task Pack的`tests`包被遮蔽）或第11节所列基线漂移；全量用例收集无导入错误。
- Ruff、文档门禁、`generate_specs.py --check`通过，`spec/`无变化；Mypy的58项错误与基线逐项相同（本机检查环境缺少openai、anthropic、textual、opentelemetry等可选依赖），本次没有新增；可读性门禁只剩第11节所列基线漂移。

## 14. 源码与测试映射

| 内容 | 位置 |
|---|---|
| 顶层CLI | [`cli.py`](../../src/harnessix/cli.py) |
| 工作流视图 | [`coding_tool_view.py`](../../src/harnessix/product_config/coding_tool_view.py) |
| Coding Context | [`agent_context.py`](../../src/harnessix/product_config/agent_context.py) |
| 草稿命名约定 | [`coding_scratch.py`](../../src/harnessix/product_config/coding_scratch.py) |
| 候选择优 | [`selection.py`](../../src/harnessix/candidates/selection.py) |
| 删除回归 | [`test_legacy_swebench_removed.py`](../../tests/evals/test_legacy_swebench_removed.py) |
| 可读性预算 | [`readability-policy-v1.json`](../../governance/readability-policy-v1.json) |
| 删除前最后版本 | [`4992c2e7`](https://github.com/carrie1988/Harnessix/tree/4992c2e7937ef7da35fad6468e0acee7940a9cbc) |
