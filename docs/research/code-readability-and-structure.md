# Coding Agent代码可读性、可维护性与结构治理研究

- 状态：已完成
- 研究范围：Harnessix Code 0.9.0
- Harnessix起始提交：`e15ffaa20142e9f61cf8412b3d4499001a695368`
- 指标定义：`harnessix.readability-report/v1`

## 1. 研究问题

本研究回答四个问题：

1. 当前源码规模、说明债务、复杂度热点、一级包依赖和公共API边界能否稳定复现；
2. 注释、命名、类型、模块拆分和自动门禁分别解决什么问题；
3. Codex、OpenCode和Claude Code研究仓库中哪些做法可由固定源码直接求证；
4. 在不改变Session、Schema、错误码、事件顺序和副作用语义的前提下，0.9.0应治理到什么边界。

本研究不以注释数量评价质量，不把文件行数直接等同于职责，也不复制参考项目代码。

## 2. 研究方法与口径

参考仓库沿用[0.2源码研究基线](baselines.md)锁定的版本：

| 项目 | 提交或版本 | 证据用途 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | 模块、公共面、变更规模和测试治理 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | 依赖方向、局部抽取、注释和Session边界 |
| Claude Code逆向仓库 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`，标称npm `2.1.88` | 仅观察重建源码规模和资料完整性限制 |

Harnessix指标由`scripts/readability_report.py`基于Python AST和Tokenizer生成。报告采用排序后的
仓库相对路径，不导入生产模块，不访问网络、数据库或环境Secret。指标定义如下：

- 物理行：`splitlines()`后的源码行数；
- 逻辑行：至少包含一个非注释、非布局Token的行数；
- 决策复杂度：基础值1，加条件、循环、条件表达式、推导式、布尔分支、`try`分支和非默认
  `match`分支；不穿透嵌套定义；
- 公共API：静态`__all__`导出的名称；其中Pydantic合同和Enum由字段、校验器及冻结Schema说明，
  其他行为类和函数要求文档字符串；
- 依赖环：`harnessix`一级包静态导入图的强连通分量。

该复杂度是稳定的风险筛选信号，不声称等价于认知复杂度、运行时复杂度或缺陷概率。

## 3. Harnessix起始基线

完整机器可读事实保存在
[`readability-0.9.0-start.json`](../baselines/readability-0.9.0-start.json)。

| 指标 | 起始值 |
|---|---:|
| Python源码文件 | 253 |
| 物理行 / 逻辑行 | 55,146 / 49,442 |
| 具备模块文档字符串 | 88 / 253 |
| 类 / 已说明类 | 667 / 139 |
| 顶层函数与方法 / 已说明函数 | 2,160 / 118 |
| 静态公共导出 | 273 |
| 公共Pydantic或Enum合同类型 | 146 |
| 未说明公共行为入口 | 56 |
| 未说明高风险入口 | 22 / 25 |
| 超过600行的文件 | 14 |
| 超长或高复杂度符号 | 148 |
| 一级包依赖边 | 164 |

### 3.1 文件热点

起始阶段的主要文件热点为：

| 文件 | 物理行 | 主要职责叠加 |
|---|---:|---|
| `agent/runtime.py` | 3,187 | 接纳、恢复、交互、Compaction、模型流、Tool和终态 |
| `agent/reducer.py` | 1,059 | Item投影、Turn迁移、模型尝试、Context和Replay |
| `delivery/git.py` | 1,036 | Worktree、Checkpoint、Commit和Git一致性校验 |
| `evals/delivery.py` | 935 | 变更包、执行、恢复与工作树发布 |
| `agent/models.py` | 927 | Agent事件及聚合合同 |
| `context/sources.py` | 838 | 多种动态Context Source |
| `processes/supervisor.py` | 823 | POSIX/Windows Owner监督与恢复 |

`agent/runtime.py`虽然最大，但内部方法与构造依赖高度共享，并直接处于取消、审批、模型流和未知
副作用交界。没有先建立稳定门面、特征测试和职责协作者之前，按行数拆分会增加跨对象隐式状态，
不适合作为首个结构手术。

`agent/reducer.py`是更安全的首批对象：它是纯事件投影，不执行I/O；Item、Turn状态和Replay已经
形成可辨识的职责簇；在线提交与离线重放又共享同一`apply_event`，现有Session、恢复和Schema
测试能够验证等价性。

### 3.2 符号热点

起始报告中的最高决策复杂度包括：

| 符号 | 行数 | 决策复杂度 |
|---|---:|---:|
| `agent.reducer:_change_state` | 223 | 90 |
| `agent.reducer:_start_item` | 260 | 82 |
| `agent.models:EventDraft.legacy_event_boundary` | 107 | 69 |
| `agent.runtime:AgentRuntime._execute_calls` | 269 | 62 |
| `agent.runtime:AgentRuntime._sample_events` | 210 | 60 |
| `agent.runtime:AgentRuntime.__init__` | 170 | 58 |

这些符号首先需要明确不变量和防增长预算。0.9.0不把复杂函数机械切成只能被调用一次的小函数；
只有能命名独立职责、缩小依赖或形成可单独验证边界时才提取。

### 3.3 依赖事实

一级包图存在一个由`agent`、`artifacts`、`context`、`execution`、`models`、`patches`、
`processes`、`secrets`、`session`、`tools`和`workspace`构成的强连通分量。其成因同时包含：

- 领域合同与运行时编排仍位于同一一级包；
- Artifact、Context和Tool Result需要读取Agent事实；
- Agent Runtime又依赖这些领域端口；
- 部分模块为避免启动期循环导入使用函数内导入。

该环不是0.9.0可安全一次消除的单点缺陷。门禁冻结现有边和强连通分量，禁止未经ADR新增依赖；
后续应优先把纯合同移向无运行时反向依赖的稳定层，而不是通过延迟导入掩盖结构问题。

## 4. 参考实现事实

### 4.1 Codex

固定提交的`codex-rs/core/src/lib.rs:1-18`以根模块说明、私有`mod`和显式公开导出组织crate，
并在`lib.rs:3-6`通过lint禁止库代码直接写stdout/stderr。`AGENTS.md:14-22`要求调用点自描述、
新Trait说明角色；`AGENTS.md:29-35`要求整对象断言、小公共面和Schema同步；
`AGENTS.md:44-61`明确反对单次使用的小Helper，并给出500/800行的模块增长边界；
`AGENTS.md:64-70`定义格式、专项测试和完整测试顺序；`AGENTS.md:72-89`又单独指出核心crate
膨胀风险和公共API最小化。

可借鉴的是“自描述调用点、私有实现、显式导出、按职责新增模块、Schema与测试门禁”。不能直接
复制固定行数：Harnessix使用Python，合同类、状态机和测试组织方式不同，现有热点必须采用
已评审债务预算渐进收敛。

### 4.2 OpenCode

固定提交的根`AGENTS.md:25-31`反对提前抽取单次Helper；`AGENTS.md:59-64`限制导入别名和星号
导入；`AGENTS.md:98-119`要求复杂主流程保持Happy Path、Helper贴近调用点，只为真实概念抽取，
注释只解释非显然约束；`AGENTS.md:141-149`要求尽量测试真实实现并使用项目统一类型检查入口。
同一文件`AGENTS.md:153-160`把持久Prompt准入、Session执行所有权、模型调用和历史重载等
关键不变量集中写在邻近开发规范中。

与此同时，固定提交中的`session/session.ts`、`session/prompt.ts`和`provider/provider.ts`分别
约1,016、1,631和2,068行。事实表明主流项目也会产生中心热点；借鉴其边界说明不能替代
Harnessix自己的复杂度与依赖防增长机制。

### 4.3 Claude Code逆向仓库

该仓库`README.md:1-7`声明源码从npm `2.1.88`包重建，`README.md:70-74`明确有108个
Feature-gated模块缺失且无法从发布物恢复。固定提交中`src/main.tsx`约4,683行、
`src/screens/REPL.tsx`约5,005行、`src/cli/print.ts`约5,594行、
`src/utils/sessionStorage.ts`约5,105行。

这些数据只能证明该研究材料包含大规模重建文件，不能据此推断Anthropic内部源码规范，也不能
把重建后的文件边界作为生产架构样板。Harnessix仅使用其中可交叉验证的行为线索，不复制源码。

## 5. Harnessix独立结论

1. 先建立可复现报告和防增长预算，再补说明或拆分，否则无法区分改善与指标漂移；
2. 模块说明、公共行为说明、高风险失败/恢复说明是三种不同义务；Pydantic合同类不能仅为
   注释覆盖率增加文档字符串，因为类文档字符串会改变JSON Schema的`description`；
3. 注释解释职责、边界、原因和不变量；显然的赋值、循环和字段名依靠命名、类型及结构自描述；
4. 公共`__all__`、超大文件/符号、一级包依赖和依赖环采用精确快照与只减不增预算；
5. `agent.reducer`保留稳定门面，Item、Turn和共用守卫可提取；`AgentRuntime`本阶段只补边界说明
   和冻结增长，不在缺少协作者契约时大拆；
6. 任何后续热点拆分必须单独提交，先有特征测试，保持Schema、错误码、事件顺序、持久化和
   外部副作用不变。

## 6. 可复查命令

```bash
uv run python scripts/readability_report.py
uv run python scripts/readability_report.py --check --check-final-report --quiet
uv run pytest tests/governance/test_readability_policy.py
git -C "$HARNESSIX_RESEARCH_ROOT/codex" show \
  a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67:AGENTS.md
git -C "$HARNESSIX_RESEARCH_ROOT/opencode" show \
  69c172e8a7c0086887b1f93ed5a162f14b6aa0c5:AGENTS.md
git -C "$HARNESSIX_RESEARCH_ROOT/claude-code-source-code" show \
  2ca5ddabfed5f220812ea11f029eda03b21bc4c1:README.md
```
