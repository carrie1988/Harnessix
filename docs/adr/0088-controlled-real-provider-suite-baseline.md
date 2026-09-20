---
doc_type: adr
status: reviewing
version: 2
code_revision: pending
owners:
  - core
modules:
  - evals
  - models
  - sandbox
related_adrs:
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
  - docs/adr/0086-formal-eval-case-adapter-and-recorded-provider-boundary.md
  - docs/adr/0087-deterministic-offline-eval-suite-composition.md
related_tests:
  - tests/evals/test_provider_suite_contracts.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
  - tests/evals/test_provider_suite_evidence.py
  - tests/evals/test_task_pack_execution.py
supersedes: []
---

# ADR-0088：复用唯一Agent主链执行受控真实Provider完整Suite

## 状态

评审中。实现候选已完成本地合同、恢复与脱敏回归；在提交、全矩阵CI和受控真实运行证据完成前，不接受本文，也不关闭0.9.2e。

## 背景

0.9.2d已经形成3仓、10 Case、20 Trial的确定性离线基线。其Recorded Provider只负责按固定Golden驱动正式Agent和
产品Action链，不能证明真实模型能够理解未知任务并完成修改。0.9.2e需要在固定模型、价格、地域、Token和费用范围内执行同一完整Suite。

直接增加Eval专用Agent、HTTP Worker或脚本式模型循环，会重新引入已经由ADR 0081移除的第二套执行面；直接把端点和凭据引用写入公共Suite Plan，又会破坏Provider中立报告和公开证据边界。

## 决策

### 1. 不新增Runner、Agent或Action执行面

真实Suite必须沿用：

`run_coding_eval_suite → TaskPackCaseExecutor → run_task_pack_coding_eval → Agent Runtime → 产品Trusted Action → Campaign/Grader`。

新增组件只负责：私有运行配置、真实Provider工厂、宿主绑定校验、显式启网CLI和低敏证据发布。

### 2. 公共Suite合同保持Provider中立

`CodingEvalSuitePlan`继续只保存公开环境身份。真实端点、API Key环境变量名、宿主Git/Container程序和源码根进入
`CodingEvalProviderSuiteRunConfig`私有合同。完整私有配置SHA-256同时绑定Suite状态和Case执行指纹；恢复时任何字段变化均失败关闭。

离线调用不传宿主绑定，原有Execution Fingerprint保持不变，避免使已冻结离线证据失效。

### 3. 默认禁网，显式启网

`harnessix coding-eval-suite`即使收到`--config`也只有同时收到`--allow-network`才读取文件和环境。配置必须是0600普通文件，拒绝符号链接、特殊文件、空文件、超限内容、模糊JSON和类型强转。

API Key只保存环境变量名。配置、CLI输出、公开证据和日志均不得保存或回显凭据值。

### 4. 固定执行和计价范围

首个基线固定：

- Pack：`harnessix-engineering/v2`，10 Case，每Case 2 Trial；
- Provider：`openai_chat`；模型：`qwen3-coder-plus-2025-09-23`；地域：`cn-beijing`；
- 隔离声明：Provider使用显式宿主网络，代码检查使用固定Container；
- 串行Tool Call、`max_attempts=1`、`retry_delay_seconds=0`、单请求最多4096输出Token；
- 不超过32K输入的价格快照：输入4元/百万Token、输出16元/百万Token；
- 人民币40元本地停止线，在完整Trial边界生效。

超过价格输入区间、Usage缺失或币种/价格不完整时，成本为未知并停止后续请求，不跨价格区间猜算。

### 5. 每Trial独立Provider生命周期

每个Trial创建独立`OpenAIChatProvider`和HTTP Client，不跨Session共享Provider状态、历史或连接生命周期。Suite仍顺序执行，避免并发改变费用停止和证据前缀语义。

### 6. 私有事实与公开证据分离

私有运行根持有Suite状态、Campaign、Session、Artifact、Workspace和Container输出。公开证据根必须与其互不包含，只发布：

1. 严格重读后的`CodingEvalSuitePlan`；
2. 严格重读后的完整`CodingEvalSuiteReport`；
3. 白名单`CodingEvalProviderSuiteEvidenceManifest`。

发布器递归拒绝Prompt、Response、Arguments、Tool Output、Diff、Workspace、Secret、POSIX/Windows绝对路径和调用方指定私有标识。成本不完整不得发布。

### 7. Agent行为缺失必须进入评分，不能使Runner崩溃

真实模型可能不调用或只调用一次固定Profile。终态Turn出现这类行为时，Task Pack Adapter必须保留空或已观察到的
Baseline/Final集合，并交给现有严格Grader；不得合成Return Code、不得在模型结束后旁路执行Profile，也不得抛出
`eval_baseline_missing`使整个Suite丢失Usage与成本。`CodingEvalRunState.baseline_observations`的v1约束因此向后兼容地允许空集合；
既有Grader会把缺失集合判为`invalid/failed`，终态Session可在不重开Provider的前提下重算并发布报告。
如果模型已经调用Profile但结果缺少可信Process终态，则仍以`eval_baseline_invalid`失败关闭；执行链故障不能降级成质量分数。

### 8. 默认CI不使用真实凭据

所有合同、恢复、CLI和脱敏行为在离线CI中验证。真实Provider运行是独立、显式、可审计的人工触发验证，不向默认CI注入Secret，也不因CI通过推导真实模型质量。

## 替代方案

| 方案 | 结论 | 原因 |
|---|---|---|
| 独立Eval HTTP/Worker | 拒绝 | 复制Agent/Action状态机，与单产品边界冲突 |
| 直接脚本调用模型并写报告 | 拒绝 | 绕过Session、审批、Tool、Usage与恢复事实 |
| Provider配置进入公共Plan | 拒绝 | 泄漏私有宿主信息，破坏公开Provider中立合同 |
| 全局复用单一Provider Client | 拒绝 | Trial生命周期和故障归属不独立 |
| 动态按供应商最新价格计费 | 拒绝 | 运行不可复现，恢复期间价格可能漂移 |
| 私有配置摘要绑定现有Runner | 采用 | 最小扩展且保持唯一权威主链 |

## 后果

### 正面

1. 真实质量证据与离线基线共享完全相同的产品执行链；
2. 重开无法静默切换端点、模型限制、凭据引用或宿主程序；
3. 默认不访问网络、不读取凭据，收费行为有显式操作边界；
4. 公开证据可重算且不复制用户/模型/代码正文；
5. 离线已冻结指纹和证据保持兼容。
6. Agent跳过测试时仍能形成严格失败报告、Token和费用证据，而不是误报评测Runtime故障。

### 代价

1. 私有配置必须随Run保存并0600保护，恢复不能重新生成；
2. 单价区间外请求会使完整Suite停止，需要新价格快照和显式恢复策略；
3. 本地费用停止线不是账单硬上限，当前Trial可能造成小幅越线；
4. 每Trial独立Client牺牲少量连接复用性能，换取生命周期隔离。

## 验收条件

本文只有在以下条件全部满足后转为`current`：

1. 私有配置、Schema、CLI、恢复绑定、Provider工厂和证据发布器已合入；
2. 默认禁网路径证明不会读取配置、环境或创建Provider；
3. 配置/Pack/源码Revision/程序/Provider字段漂移均失败关闭；
4. Suite与Case恢复指纹专项回归通过，离线旧指纹不变；
5. Prompt、回答、工具正文、路径、Secret和私有运行身份泄漏回归通过；
6. 缺失Profile调用可生成严格失败报告，完成状态允许空Baseline事实且恢复不重开Provider；
7. Ruff、Mypy、Schema、文档、Pytest与全矩阵CI通过；
8. 固定模型完成或按合同停止完整10 Case × 2 Trial真实Suite；
9. 低敏报告经严格重读和重算后冻结，实际费用不超过授权范围；
10. 路线图、架构、Evals模块、测试规范、运维和验证索引同步。

## 关联资料

- [受控真实Provider完整Suite研究](../research/controlled-real-provider-suite.md)
- [0.9.2e详细设计](../changes/m09-2e-controlled-real-provider-baseline.md)
- [真实Provider Suite运维手册](../operations/provider-suite-baseline.md)
- [Evals模块设计](../modules/evals.md)

## 被取代关系

无。本文扩展ADR 0048的单任务真实Campaign和ADR 0087的完整离线Suite，不取代其合同与证据。
