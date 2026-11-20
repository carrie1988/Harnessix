---
doc_type: test-and-eval-design
status: current
version: 3
code_revision: 684e0a4b27a8dd4a48227c08d6ec05bb66735cc4
owners: [core]
modules: [evals]
related_adrs:
  - docs/adr/0120-system-neutral-evaluation-platform.md
  - docs/adr/0132-append-only-requests-and-token-calibrated-compaction.md
related_tests:
  - tests/integrations/test_golden_eval_set.py
supersedes: []
---

# 黄金评测集（第1版）

用于版本指标评测的固定题集。只有预期会明显影响任务解决率或任务耗时的版本才评测，不按版本定期评测
（2026-11-14决定）。每次评测用同一组题、同一套条件，结果才能与历史基线比较。

机器可读的清单是[`benchmarks/harbor/golden-v1/golden.json`](../../benchmarks/harbor/golden-v1/golden.json)：
它引用两份已锁定的题目文件并记录其SHA-256，[测试](../../tests/integrations/test_golden_eval_set.py)
保证清单、锁定文件与剔除名单三者一致，任何一处被改动都会失败。

## 1. 组成

| 部分 | 作用 | 题目 | 有效题数 | 每个系统的运行次数 |
|---|---|---|---|---|
| `multilingual-test28` | 主指标 | SWE-bench Multilingual中以随机种子20261111抽取的30题（[选题记录](../../benchmarks/harbor/swebench-multilingual-round2/selection.json)：按语言分层、每仓库至多2题、排除题面带修复PR链接或补丁的题），覆盖Java、Go、Rust、C/C++、JavaScript/TypeScript、PHP、Ruby共22个仓库 | 28 | 3 |
| `verified-dev23` | Python回归检查 | SWE-bench Verified的25题（[选题记录](../../benchmarks/harbor/swebench-verified-dev25/selection.json)） | 23 | 1 |

两部分都取自Harbor数据集提交`37db1088`。剔除的题对所有系统一律不计：

| 题目 | 原因 |
|---|---|
| `axios__axios-4738` | 标准答案在本评测环境里通不过官方判分 |
| `fluent__fluentd-3641` | 必过测试需要IPv6，断网的任务网络没有IPv6，任何系统都过不了 |
| `psf__requests-1921`、`psf__requests-2317` | 测试要访问httpbin.org，任务断网运行，标准答案同样失败 |

## 2. 评测条件

条件不同的结果不能与下面的基线比较。下列任何一项变化（模型、权重、推理服务、思考模式、采样参数、预算、
判分程序），都要先在新条件下重跑基线（至少mini-swe-agent和上一个评测过的版本），再做比较。

| 项目 | 条件 |
|---|---|
| 模型 | Qwen3.8-Flash-Next的INT8量化版本（W8A8：权重按通道、激活按Token动态量化） |
| 推理服务 | vLLM 0.29.0，8张海光DCU K100_AI上2个副本（每副本4卡张量并行）；最大上下文262144 Token；前缀缓存；MTP投机解码（1个投机Token）；每副本至多32个并发请求 |
| 思考模式 | 所有系统关闭（`enable_thinking=false`） |
| 采样参数 | 请求中不指定，使用模型默认值temperature=1.0、top_p=0.95、top_k=20 |
| 并发 | 同时4个任务，每个任务固定使用一个副本 |
| 每个任务的预算 | 最多100次模型调用、60分钟、单条命令900秒、单次模型请求1800秒。Harnessix单次回复最多16384个输出Token、累计1000万Token、声明上下文262144；mini-swe-agent不设单次输出上限与费用上限 |
| 运行环境 | 命令在任务容器内执行，Agent阶段断网；Harnessix无人值守、自动批准；mini-swe-agent为yolo模式 |
| 对照系统 | mini-swe-agent 2.4.6，自带SWE-bench配置，只改上面的预算并关闭思考 |
| 调度 | Harbor 0.25.0 |

## 3. 判分与报告

- **解决率**：SWE-bench官方判分（swebench 5.0.2），对导出的完整补丁判定；官方标为ambiguous的失败计为未解决。
- **耗时**：每个任务从Agent开始到结束的实际用时，包含模型推理与命令执行；只能在同一环境内比较。
- **无效运行**：以基础设施故障结束的运行不计入分子分母，单独报告数量与原因。
- **比较**：按题配对；对题目做10000次重采样得到95%区间，另给出按题的符号检验；解决率与耗时一并报告。
  28题时解决率的区间约为±13个百分点，小于10个百分点的变化测不出来，报告里要写明。

## 4. 基线

| 部分 | 系统 | 测量日期 | 解决（官方判分） | 平均耗时 |
|---|---|---|---|---|
| multilingual-test28 | Harnessix 2026-11-13构建（第二轮候选，随0.10.0b41发布） | 2026-11-13 | 47/84（56.0%） | 20.9分钟 |
| multilingual-test28 | Harnessix 2026-11-10构建 | 2026-11-12～13 | 27/55（49.1%） | 29.0分钟 |
| multilingual-test28 | mini-swe-agent 2.4.6 | 2026-11-12～13 | 33/79（41.8%） | 15.9分钟 |
| verified-dev23 | Harnessix 2026-11-13构建 | 2026-11-14 | 16/23 | 15.4分钟 |
| verified-dev23 | Harnessix 2026-11-10构建（单次回复输出上限4096） | 2026-11-10 | 17/23 | 15.2分钟 |
| verified-dev23 | mini-swe-agent 2.4.6 | 2026-11-10 | 17/23 | 7.6分钟 |

## 5. 使用规则

1. **只用于版本评测。** 调优用开发集，不用黄金评测集。
2. **不按题调优。** 评测后只看汇总结果；逐题结果只用于查明异常结束和通用缺陷（崩溃、协议错误等）的根因。
   由逐题结果引出的任何产品改动，都要记入清单的`exposure_log`。
3. **记录每次使用。** 每次评测在`exposure_log`追加一条：日期、用途、是否按题分析过。
4. **何时更新。** 出现下列情况之一时制作第2版：按题分析过的题累计超过三分之一；需要识别小于10个百分点的
   变化；评测条件长期改变（例如换默认模型）。
5. **版本规则。** 第1版冻结后不再修改题目与剔除名单；扩充或更新另建`golden-v2`，并在新版本上重跑基线。
   不同版本的数字不能直接比较。

## 6. 已知情况

- `multilingual-test28`在2026-11-13正式评测时用过一次。之后看过汇总结果、复核了从未正常结束的4题
  （terraform-34900、php-cs-fixer-7663、lombok-3326、valkey-790），并对全部标准答案做了官方判分校验；
  没有据此修改产品。
- `verified-dev23`在2026-11-10的正式对比中逐题分析过失败原因，属于“见过的题”，只作Python回归检查。
- 每次评测的运行配置按第2节条件另行生成；仓库里第二轮的运行配置文件是当时的版本，复用时要核对Agent构建与配置。
- 2026-11-20起每次评测的任务容器都有32 GB内存上限（[评测集成运维](evaluation-integrations.md)第7节第4步）：
  共用主机上一个失控试次曾触发整机内存耗尽。正常任务远用不到这个量，可比性不变；因超出上限被结束的命令
  算该试次自己的结果，不按无效运行剔除。
- 两个系统同时运行时（2026-11-18速度轮按负责人决定改为各4个并发、同时跑），各运行的方案名（arm id）必须不同，或者错开官方判分：官方判分以方案名与尝试序号作运行名，容器名为`sweb.eval.<题号>.<运行名>`，两个运行同时判分时同名容器互相停掉，表现为`error`（2026-11-18 `verified-dev23`候选6个试验）。受影响的试验用同一份预测、唯一的运行名单独重判，并在使用记录里写明。
