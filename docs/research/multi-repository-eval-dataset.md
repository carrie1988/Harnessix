---
doc_type: source-research
status: current
version: 1
code_revision: 0245d117adc7c385a4e42de4e023fd0d22bbb1cd
owners:
  - core
modules:
  - evals
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
  - docs/adr/0085-versioned-third-party-eval-dataset-and-golden-boundary.md
related_tests:
  - tests/evals/test_engineering_task_pack.py
  - tests/evals/test_task_pack.py
  - tests/integration/test_task_pack_profiles.py
supersedes: []
---

# 多仓库离线Eval数据集源码研究

## 1. 研究目标

本文为0.9.2d固定多仓库离线基线的数据来源、许可证、任务形态和确定性Oracle边界。研究回答四个问题：

1. 数据集能否覆盖Coding Agent常见的Bug Fix、Feature、Refactor、Test和Review，而不是十个同质函数修复；
2. 第三方源码能否以明确Revision、许可证和版权通知进入可分发的内置Task Pack；
3. Review任务如何在不使用LLM Judge的前提下绑定真实源码证据；
4. 黄金答案如何只用于数据集自检，而不泄漏给运行中的Agent。

研究不复制三个项目的Agent Runtime、Prompt或私有协议。Claude Code逆向样本不作为数据来源，也不进入发行物。

## 2. 固定来源与许可证证据

| 来源 | 固定Revision | 研究文件 | 许可证 | 数据集用途 |
|---|---|---|---|---|
| OpenAI Agents Python | `8dfac2aeec56f9f833b316e66f075bd888930646` | `src/agents/util/_json.py`、`_transforms.py` | MIT，Copyright (c) 2025 OpenAI | 工具名、可序列化转换、载荷与日志脱敏边界 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | `packages/core/src/util/wildcard.ts`及相邻工具 | MIT，Copyright (c) 2025 opencode | 跨平台路径、重试退避和终端URL安全边界 |
| LangChain | `54a9556f5c89dc734f906aa084426e8a238691ac` | `libs/core/langchain_core/utils/strings.py`、`iter.py` | MIT，Copyright (c) LangChain, Inc. | 字符串化、存储清洗和迭代批处理边界 |

三个许可证正文逐字节进入各自Benchmark Archive，Manifest同时保存许可证SHA-256、版权通知、上游永久链接和
审查时间。来源文件被缩减为只依赖Python/Node标准库的派生夹具，并故意注入评测缺陷；它们不代表上游当前行为。

## 3. 源码观察

### 3.1 OpenAI Agents Python

`_transforms.py`把空格及非法字符规范为下划线并统一小写；`_json.py`递归处理字典、序列和一般Iterable，同时把
字符串与字节排除在一般Iterable转换之外。可迁移到数据集的不是实现文本，而是以下工程约束：

- 合法函数名也必须执行同一大小写规范，避免分支导致身份漂移；
- `list`与`tuple`行为一致，可用于验证等价重构；
- `bytes`不能被当作一般Iterable展开；
- 诊断路径不能原样返回Provider凭据。

### 3.2 OpenCode

`wildcard.ts`先把Windows反斜线规范为正斜线，再构造匹配表达式，并根据平台选择大小写行为。它提供了可离线验证的
跨平台路径任务。Coding Agent还经常处理重试和终端URL，这两类边界适合构造Refactor和Security Review：前者要求
行为等价，后者要求清除URL userinfo而保留协议、主机、路径与查询。

### 3.3 LangChain

`strings.py`展示嵌套值字符串化及NUL字节清理，`iter.py`展示`size=None`时一次消费全部输入的批处理行为。由此得到：

- 非字符串字典键必须显式转换，不能依赖字符串拼接隐式强转；
- 调用方提供的replacement必须影响NUL替换结果；
- 测试任务应验证边界而非修改已有正确实现。

## 4. 数据集结构决策

`harnessix-engineering/v1`固定三个派生Benchmark仓库、十个Case和十个Case专用Profile：

| 仓库 | Bug Fix | Feature | Refactor | Test | Review |
|---|---:|---:|---:|---:|---:|
| agents-utils-benchmark | 1 | 0 | 1 | 1 | 1 |
| opencode-utils-benchmark | 0 | 1 | 1 | 0 | 1 |
| langchain-utils-benchmark | 1 | 1 | 0 | 1 | 0 |
| 合计 | 2 | 2 | 2 | 2 | 2 |

每个Case只允许修改一个路径。Case专用检查避免同一仓库内其他故意缺陷影响当前结论；Profile固定Digest镜像、程序、
参数、CPU、内存、进程、输出和超时，网络恒为`none`。测试任务的目标路径在基线中不存在，由检查器要求新增回归测试并
实际执行；Review任务同时要求修复和报告固定Finding ID。

## 5. Review Oracle证据口径

Review Finding的`evidence_sha256`不是描述文本摘要，而是以下确定算法的结果：

```text
body = read_regular_utf8_file(finding.path)
lines = body.splitlines(keepends=True)
evidence = concat(lines[start_line - 1 : end_line])
sha256(evidence) == finding.evidence_sha256
```

Archive首次物化时必须在创建Git提交前执行该校验。路径需解析为Workspace内普通文件，UTF-8、行范围和摘要任一不匹配
均返回`eval_task_pack_review_oracle_invalid`并删除未完成Run目录。Oracle由Pack摘要保护，Archive由独立摘要保护，
二者不能单独漂移。

## 6. 黄金补丁与运行时隔离

黄金补丁位于[`benchmarks/taskpacks/harnessix-engineering-v1/solutions`](../../benchmarks/taskpacks/harnessix-engineering-v1/solutions)，
不位于`src/harnessix`，并由构建配置显式排除出sdist，因此不进入Wheel或源码安装制品。它只用于以下发布前证明：

1. 每个固定Profile在原始Archive上非零退出；
2. 只应用该Case黄金补丁后零退出；
3. Git变更集合精确等于`allowed_changed_paths`；
4. 检查器没有要求修改未授权路径。

正式Agent运行只加载Manifest和Archive，不能通过Task Pack Catalog、Workspace或模型上下文读取黄金补丁。0.9.2d2/d3的
Case Adapter也不得接收Solution路径。

## 7. 采用与拒绝

| 机制 | 结论 | 原因 |
|---|---|---|
| 固定Revision的MIT派生夹具 | 采用 | 来源、许可证和内容可审计，且可离线运行 |
| 直接打包三个完整上游仓库 | 拒绝 | 体积、依赖和供应链面远超当前任务目标 |
| 从网络动态下载Revision | 拒绝 | 破坏离线可复现性并扩大凭据与供应链风险 |
| 一个Profile运行仓库全部故意缺陷 | 拒绝 | 无法隔离Case因果，单任务修复仍会失败 |
| Review使用LLM Judge | 拒绝 | 不确定、额外收费且难以重算 |
| Review只匹配自由文本 | 拒绝 | 容易误判，不能绑定源码证据 |
| 黄金补丁进入Wheel | 拒绝 | 会泄漏答案并削弱Eval可信度 |
| Claude Code逆向样本作为夹具 | 拒绝 | 非官方来源，不满足可分发权利链要求 |

## 8. 风险与后续验证

- 小型工具夹具不能代表大型跨模块仓库；0.9.3后续增加任务时必须发布新Pack版本，不改写v1；
- AST型Refactor检查只证明本Case要求，不代表一般代码质量评分；
- d1只证明数据和检查闭环，尚未证明真实Agent能完成任务；该证明由0.9.2d2/d3和0.9.2e承担；
- 第三方许可证在0.9.4正式SBOM和发行物通知门禁中再次核对。

## 9. Go/No-Go

结论：**Go，限定为版本化派生Benchmark数据。** 三个来源具备明确MIT权利链，十个Case可在固定Python/Node镜像中
无网运行，Review证据可确定重算。不得把本数据集存在本身解释为Coding Agent质量基线已经完成。
