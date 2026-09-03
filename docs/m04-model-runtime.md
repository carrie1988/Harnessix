# 0.4 Model Runtime 实施计划

- 日期：2026-09-03
- 状态：0.4.1、0.4.2a、0.4.2b1/b2、0.4.3a/b1/b2 已完成离线验收；百炼文本/内存工具/审批重开实测通过，真实计价适用性验收未完成
- 依据：ADR 0008、当前 ModelProvider/ProviderEvent 契约与主路线图

## 1. 目标与边界

让同一 Kernel 切换 OpenAI-compatible 与 Anthropic Provider，而不依赖供应商消息对象。不在本阶段开放写工具或绕过审批，不以真实 API 能返回文本作为整个 Coding Agent 完成的证据。

以小切片推进，每次先核对官方文档、当前 SDK 源码/版本和测试能力，不猜测流事件或重试行为。

## 2. 0.4.1：接口求证与首个 Provider

本切片已实现；接口选择、完整状态语义与约束见 [ADR 0014](adr/0014-openai-compatible-provider.md)。官方 SDK 作为可选依赖，版本范围 `openai>=2.54,<3`，当前锁定 2.54.0；不依赖 SDK 私有方法。

设计交付：

- 选定 SDK/传输方案并记录版本、接口来源与取消/重试语义；
- Provider 配置、能力矩阵、模型标识和 Secret 读取边界；
- 内部 Item 到供应商消息的映射，尤其是 Tool Call/Result 的稳定身份与跨步骤配对；
- 文本流、参数分片、结束事件、Usage 和错误的映射表；
- 超时/输出预算与取消时连接关闭的规则；
- 重试只限“尚未向 Kernel 暴露部分响应或 Tool Call”的安全边界，避免 SDK 隐式重试与 Kernel 控制叠加。

实现交付：

- 首个 OpenAI-compatible Adapter；
- 可注入的测试传输/客户端与脱敏诊断；
- 默认离线的共享 Provider 契约，包括坏 JSON、截断、重复结束、取消和限流；
- 不将 API Key、HTTP Header 或原始 SDK 异常写入 Session/Trace。

## 3. 0.4.2：第二类 Provider 与能力差异

按 Adapter 与账本/SDK 接入分片验收，见 [ADR 0015](adr/0015-anthropic-provider.md)：

- **0.4.2a 已完成**：Anthropic Messages Adapter、HTTPX2 类型隔离、共享 Provider 契约、缓存计数纳入输入总量、跨 Provider 继续与审批重启；
- **0.4.2b1 已完成**：Model Attempt 身份、完整/部分/未知累计用量、明细包含关系、失败结算与版本化持久记录，归一化 Provider 验收通过；
- **0.4.2b2 已完成离线验收**：两个实际 SDK 的尝试元数据、缓存/推理/失败用量映射、共享契约、取消与 8 个进程崩溃切点；设计见 [ADR 0017](adr/0017-provider-attempt-usage.md)。不把 SDK + HTTP 替身通过写成真实平台已验收。

整体交付要求：

- Anthropic Adapter 接入同一契约；
- 明确 capability 不支持时的行为，不静默丢弃不支持的 Item 或伪造 Usage；
- 保留公开 Reasoning Summary 与私有推理内容的边界；
- 增加缓存、推理 Token 等供应商差异的结构化表示，不能直接改变 Kernel 主循环；
- 两个 Adapter 通过相同的核心 Contract，额外差异由各自测试覆盖。

## 4. 0.4.3：受控真实验证与阶段验收

- 真实 Smoke Test 与默认 CI 分离，凭据仅由运行环境注入；
- 首先验证文本与只读工具闭环，设置请求次数、Token、超时和费用边界；
- 核对实际 Usage、响应中断、取消清理、审批后继续与日志/Trace 内容；
- 提交可复现测试命令、脱敏结果和剩余兼容性限制；
- API 认证、额度或网络不可用时记录为外部阻塞，继续独立的离线契约测试，不将 Mock 通过写成真实平台验证通过。

## 5. 完成条件

主路线图中 0.4 的 Provider、能力、认证、错误/重试、取消、Token/成本和脱敏诊断均需有实现与测试证据；两类 Adapter 共享契约通过，并完成受控真实平台验证。2026-09-03 按用户要求调整实施顺序：三场景功能已实测通过，计价证据独立待验收，允许先推进 0.5 离线切片，但不据此宣称 0.4 整体完成。

当前不需要安装远程中间件。已核对百炼北京端点与固定非 Thinking 模型进行小预算验证，结果见第 15 节；凭据仅在本次进程内注入，默认测试无需 Key。后续确有 PostgreSQL 等需求时评估用户提供的远程主机，不将密码或 API Key 写入仓库。

## 6. 当前 API 与边界

~~~python
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider

config = OpenAIChatConfig(
    model="configured-model-id",  # 示例占位，使用时必须替换为实际 ID
    base_url="https://api.openai.com/v1",
    api_key_env="OPENAI_API_KEY",
    max_output_tokens=1024,
    max_attempts=2,
)
# 在异步宿主中：
# async with OpenAIChatProvider(config) as provider:
#     async with AgentRuntime(store, provider, tools) as runtime:
#         ...
~~~

`OpenAIChatConfig` 必须在启动前完成校验，使用环境变量**名称**而不是 Key 值。对于兼容端点，需核对地域、URL、模型、流式 Usage 和 Token 参数后显式配置；当前没有自动端点探测或模型能力猜测，也不自动加载 `.env`。配置 Schema：`spec/openai-chat-config-v1.schema.json`。

| 能力 | 当前结果 |
| --- | --- |
| Chat 文本、工具调用、并行参数组装 | 已实现，离线契约通过；可禁用工具/并行能力 |
| History 配对、工具别名、审批继续 | 已实现，实际 SDK + Kernel/SQLite 测试通过 |
| 输入/输出总 Token | 完整 Usage 归一化；length/filter 等失败终态保留已知 Usage |
| 尝试与失败用量 | 两个 SDK 接入当前 v3 元数据；意图先持久化，重试独立记录；失败/取消/恢复不清零 |
| 请求输出预算 | min(剩余 Turn Token、配置输出上限)；不能精确预扣输入 Token |
| 认证与错误 | Secret 环境引用；闭集 code/retryable，不输出原始异常 |
| 取消与超时 | 建连、读流、错误 body、退避和调用方 Task；响应关闭 |
| Reasoning / 多模态 / 内置工具 | 未开放；不透传私有推理，公开摘要 Item 暂不接受 |
| 缓存/推理 Token、价格、失败请求费用 | 明细映射与显式价格绑定的事后估算已实现；缺失信息保持未知，不等同于供应商账单 |
| Anthropic | 0.4.2a 已通过离线验收，具体严格配置见下节 |
| 受控 Smoke / 白名单诊断 | 已实现，实际 SDK + 离线传输验收；百炼文本/内存工具/审批重开实测通过，范围见第 15 节 |
| 响应计费元数据 | 0.4.3b2 已实现服务等级、推理地域、5m/1h 分项持久化；只对显式匹配直连平台解析上下文 |

流式成功要求 finish reason、Usage 和 `[DONE]`，不支持缺失流式 Usage 的兼容服务。SDK 在终结符后停止读取；同一已读块中的额外终结数据会拒绝，不承诺读取终结符后所有网络字节。

## 7. 0.4.1 离线验收快照

- `tests/contracts/provider.py`：供应商中立的 11 条共享行为契约，目前由 OpenAI Adapter 工厂实例化；
- `tests/models/`：101 条测试，包含真实 SDK、MockTransport、坏 JSON/尾包、资源边界、HTTP 错误 body 清理、认证隔离和 Kernel 多步骤/审批/Replay；
- `examples/kernel_openai_offline.py`：真实 SDK→Provider→Kernel→SQLite 的可运行离线入口，不调用真实 API；
- `make check`：280 passed、1 skipped（本地未配置 PostgreSQL）；Ruff 与 Mypy 通过；
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/models tests/agent -W error`：244 passed；
- 四个 Kernel 离线入口、sdist/wheel 构建均通过；独立 Python 3.12 wheel 环境验证基础 Kernel 无 SDK 依赖，安装可选 SDK 后端到端离线验收通过；
- 旧 Agent Event/Thread/Provider Event Schema 未改变，新增配置 Schema 纳入代码一致性测试。

实现时复现并修复了 SDK 在“HTTP 错误响应尚未返回 AsyncStream、读取 body 失败或取消”路径的资源清理缺口：关闭责任下沉到有界 Transport，不只依赖 Adapter 的 `finally`。回归同时覆盖正常流和错误 body。

上述数字为 0.4.1 收口快照，后续收口结果如下，最新状态见第 14–15 节。

## 8. 0.4.2a：Anthropic 支持边界与验收

使用可选依赖 `harnessix[anthropic]`（`anthropic>=1.3,<2`，锁定 1.3.0，HTTPX2 2.12.0）。Kernel 不导入 SDK；仅安装 Anthropic extra 不要求 OpenAI SDK，反之亦然。

~~~python
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig

config = AnthropicConfig(
    model="configured-model-id",  # 替换为支持非 Thinking 配置的已核对模型
    base_url="https://api.anthropic.com",
    api_key_env="ANTHROPIC_API_KEY",
    max_output_tokens=1024,
)
# async with AnthropicProvider(config) as provider:
#     ...  # 注入现有 AgentRuntime，无须修改 Kernel
~~~

~~~bash
uv sync --locked --all-extras --dev
uv run pytest tests/models
uv run --extra anthropic python examples/kernel_anthropic_offline.py
~~~

- 支持文本、客户端工具、并行工具组、稳定 UUID 配对、取消与有限重试；不发送 assistant prefill；
- 显式 `thinking=disabled`；强制 Thinking 模型、签名块回传、服务器工具、Fallback、图片、Citations 与 Beta 功能未开放，不能任意删除这些内容后继续；
- 要求最终可确定普通输入、缓存读取/创建和输出计数；缺失缓存计数不补零，因此不支持省略这些计数的兼容响应；
- 累计 Usage 更新取最后值，不逐块相加；输入总量包含两个缓存计数；子项明细已由后续 b2 持久化；
- b2 已补充 context_overflow 和中途异常的已知用量保留；未知分项不补零；
- 配置 Schema 为 `spec/anthropic-config-v1.schema.json`；OpenAI 配置 Schema 语义保持相同（仅字段排列变化），历史事件 Schema 和 Migration 不变。

验收结果：

- `tests/models/`：219 passed，其中原有 101 项保留，新增 118 项；两类 Adapter 分别实例化同一组 11 条核心契约；
- `make check`：398 passed、1 skipped（本地未配置 PostgreSQL）；Ruff/Mypy 通过；
- 异步调试下 Kernel + Provider：362 passed，警告作为错误处理；
- 验证包含 Anthropic→OpenAI 会话切换、审批暂停后换 Provider 继续、Usage/配对/Replay、SDK 隐式类型转换、错误 body 关闭与单网络块 Ping 上限；
- 五个离线入口、sdist/wheel 构建通过；独立 Python 3.12 环境验证基础包无模型 SDK/HTTPX2 依赖，Anthropic-only 与 OpenAI-only 两种安装分别完成 SDK→Kernel→SQLite 闭环；
- 真实 Anthropic/百炼 API 调用：0，仍待受控验证。

以上为 0.4.2a 收口快照。真实验证需要宿主中可读取的凭据、地域和模型配置；这一外部条件不阻塞离线开发。

## 9. 0.4.2b1：模型尝试账本

已实现 [ADR 0016](adr/0016-model-attempt-ledger.md) 的 Kernel、领域、迁移切片。关键规则：先提交 Started 才继续 Provider；同一尝试只按累计输入/输出差额记账；ResponseCompleted 只校验对应总量并推进原有完成门禁，不能再加一次；失败/取消/中断保留最后观测。

新公共 Schema：`agent-event-v4`、`agent-thread-v4`、`provider-event-v2`。Migration 0004 不重写历史；旧 Schema 哈希冻结；从真实旧代码生成的 v3 样本与既有 v1/v2 样本一起通过混合升级。

`Turn.usage` 是已知总量下界，不是价格或账单。`Turn.usage_is_complete` 明确是否每个已开始的模型步骤都有已结算且完整的尝试记录；旧 Provider 内部尝试不可见时为 false。分项只用于明细，不能再次累加到已包含它们的总量上。

新增子进程矩阵 15 个切点：Started/Observed/Finished × 事务三切点，共 9 个；运行时提交后的 3 个；恢复结算事务 3 个。与既有 26 个切点合计 41 个。取消覆盖 CancelToken、Task Cancel、超时、异常和 EOF。

本地验收（2026-09-03）：

- `make check`：474 passed、1 skipped（PostgreSQL 本地未配置），Ruff/Mypy 通过；
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/agent tests/models -W error`：438 passed；
- 五个既有离线示例通过，未调用真实 API；
- sdist/wheel 构建、Base-only / OpenAI-only / Anthropic-only 独立安装验收通过；
- 从原提交导出真实 v3 程序，确认其初始化 v4 数据库时返回 `schema_too_new`。

以上是 b1 收口快照；后续 SDK 接入已完成，见下节。

## 10. 0.4.2b2：实际 SDK 用量账本接入

沿用 OpenAI 2.54.0 / Anthropic 1.3.0 的锁定依赖，不改变 Kernel、Migration 或公共 Schema。两个 Adapter 发出 Started/Observed/Finished；HTTP 处理器测试断言请求前已能读取持久化的运行中尝试。重试仅在尚未发布语义响应时允许，元数据本身不关闭此边界。

- OpenAI：最终 Usage Chunk 校验后立即发布完整计数，缺失 DONE、坏工具参数或后续传输失败不丢弃此前合法观测；额外校验实际模型稳定与原始 JSON 严格类型。
- Anthropic：开始/累计更新为 partial，message_stop 时按可确定计数提升 complete；迟到缓存分项允许补齐，累计值只按差额记账。
- 两者映射缓存读/写和公开推理计数；推理计数是输出子集，不代表已开放 Thinking 内容。
- 取消/生成器关闭时不额外 yield，由 Kernel 结算已提交尝试。直接消费 Provider 的调用者也需承担结算责任。
- 只记录已校验并交付 Kernel 的快照；不能据此保证已捕获远端全部消费。Adapter 类型不是计费平台标识。

本地验收（2026-09-03）：

- `make check`：**546 passed、1 skipped**（PostgreSQL 本地未配置），Ruff/Mypy 通过；较 b1 新增 72 项。
- `tests/models/`：291 项，其中两个 Adapter 各实例化同一组 12 项核心契约。
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/agent tests/models -W error`：**510 passed**。
- 新增实际 SDK 子进程切点：两类 Adapter × 意图/首用量/完整用量/完成收据，共 8 个；与既有切点合计 **49 个**，恢复不重发请求、不重复记账。
- 五个离线入口通过（统一以 `uv run python -m examples.<模块名>` 运行）；两个 SDK 入口直接验证持久尝试及缓存/推理明细。
- sdist/wheel 构建通过；独立 Base-only / OpenAI-only / Anthropic-only 环境通过，使用 `python -I` 验证实际安装包而非工作区源码。
- 真实 API 调用：**0**；没有向仓库写入真实凭据，没有新增远程中间件。

## 11. 0.4.3 分片计划

1. **0.4.3a 已完成：价格与成本事实设计/离线实现**。显式绑定快照和宿主核对的上下文，未知信息不补零；事后估算不等于实时预算硬上限，详见下节。
2. **0.4.3b 分两片交付**：**b1 已完成离线验收**，受控 Smoke/白名单诊断、文本/内存工具/审批重开三场景，默认 CI 离线；**b2 已完成离线验收**，原生计费元数据的来源、尝试绑定、持久化与计价冲突校验。未采集字段保留未知，不套用默认地域/服务等级/模式；请求值不能替代响应实际值。
3. **0.4.3c 部分验证：文本/内存工具/审批重开通过，计价适用性待收口**。运行前核对凭据环境引用、端点地域、模型能力和价格来源，保存脱敏证据。条件缺失时继续独立离线工作，不把外部阻塞误报为已完成。

本阶段不需要远程数据库。后续 0.5 按独立切片推进，当前实施顺序见第 5 节；不能据用量测试数量宣称生产级 Coding Agent 已完成。

## 12. 0.4.3a：版本化 Token 成本报告

新增 `models.pricing` / `models.costs` 纯函数模块与 [ADR 0018](adr/0018-versioned-token-cost.md)。不修改 Kernel、SDK 或 Session Migration，不新增依赖。PriceSnapshot / CostReport 各有独立 v1 JSON Schema；报告不是新的会话事件，宿主决定是否保存其 JSON。

~~~python
from harnessix.models.costs import CostReport, bind_price, build_cost_report

# turn 来自 Kernel；verified_price / verified_context 由可信宿主预先核对。
# 每次尝试独立选择适用价格。这里只绑定最后一次，其余保持未定价。
binding = bind_price(turn.model_attempts[-1], verified_price, verified_context)
report = build_cost_report(turn, (binding,))
restored = CostReport.model_validate_json(report.model_dump_json())
assert restored == report
~~~

- 单价是每百万 Token 的严格十进制字符串；整数定点计算保留精度，不在每次尝试按分舍入。
- 平台、实际模型、地域、服务等级和推理模式必须明确匹配。输入阶梯作用于完整请求，支持同价或未缓存/缓存读/缓存写分项；输出包含推理，不重复收费。
- 必要计数或 TTL 缺失、尝试仍运行、用量非 complete、窗口不覆盖时金额为 null。失败但完整观测的尝试仍可计算，不把失败当免费。
- 已知小计分币种输出；未知尝试、旧 Provider 未覆盖步骤或未终结 Turn 都使报告不完整。无尝试时保持 unknown。
- 每份报告嵌入计算必需的价格与用量快照；JSON 重载重算、哈希错绑拒绝，不复制 Prompt、错误原文、供应商响应 ID。哈希不是签名，也不证明价格来源真实。
- 当前是显式绑定后的事后 Token 估算；不支持汇率、税、折扣、缓存存储或服务器工具费，不承诺账单精确一致、实时费用硬上限或自动计费上下文采集。没有内置真实平台费率。

验收（2026-09-03）：

- `make check`：**642 passed、1 skipped**（本地 PostgreSQL 未配置），Ruff/Mypy 通过；新增 96 项价格/成本测试。
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/agent tests/models -W error`：**606 passed**；Provider/价格/成本合计 387 项。
- 两类实际 SDK 的成功、重试和 Usage 后断流均通过 Kernel/SQLite → CostReport → JSON/Replay 重算；此前 49 个崩溃切点全部保留。
- 六个离线入口与 sdist/wheel 构建通过；新增 `uv run --extra openai python -m examples.kernel_cost_offline`，费率和上下文全部为虚构夹具。
- 独立基础 wheel 在未安装任何模型 SDK 时完成失败尝试计价与 JSON 重算；OpenAI-only/Anthropic-only 环境通过既有链路，OpenAI-only 额外通过新成本入口。
- 真实 API 调用：**0**；下一步为 0.4.3b 的 Smoke、脱敏诊断和可验证计费上下文边界。

## 13. 0.4.3b1：受控模型 Smoke 与白名单诊断

实现 [ADR 0019](adr/0019-controlled-model-smoke.md)；操作说明见 [Smoke 使用说明](model-smoke.md)。新增 `smoke.contracts`、`smoke.runner`、`smoke.cli`，只在原有 CLI 中增加提前分流，不改变 Kernel、SDK Adapter、迁移、依赖或历史 Schema。

- `harnessix model-smoke --config ...` 默认禁用；CLI 不读配置文件，库不创建工厂。显式启用后只发送固定文本/内存工具请求；凭据仅使用环境引用。
- 三场景均经过真实 Kernel、私有 SQLite、关闭/重开与 Replay；approval 只自动批准内存夹具，并验证审批前执行数为 0。不是进程崩溃恢复测试或 Coding Eval。
- 默认 128 输出 Token/请求、30 秒 Turn，最大两个模型步骤，SDK/Adapter 零重试；固定正文/帧/块预算。Token 阈值不等于实际消费或金额硬上限。
- 报告仅枚举/计数/布尔值，不复制任何供应商任意字符串。CLI 不回显非法参数/JSON/异常，并在执行期间禁用标准 logging；库不修改宿主日志。
- 完整用量、内容、工具数、审批和 Replay 均满足才通过。未知计数保持 null；尝试意图不是计费成功。注入工厂明确标记 injected，不冒充真实平台证据。

验收（2026-09-03）：

- `make check`：**736 passed、1 skipped**（本地 PostgreSQL 未配置），Ruff/Mypy 通过；新增 **94 项 Smoke 测试**。
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/agent tests/models tests/smoke -W error`：**700 passed**。
- 双 SDK × 三场景的库/CLI 验收，以及认证/限流/服务错误、无重试、断流/缺 Usage、错误标记/参数/重复工具、Token/超时/取消、重开 Replay 与 canary 覆盖。
- 新增两个真实 SIGINT 子进程测试：CLI 退出码 130、Turn 已结算 cancelled、无 stderr 原文、临时目录已清理；既有 **49 个事务/进程崩溃切点**保留，不把 SIGINT 计入这 49 个。
- 六个既有离线入口、sdist/wheel 构建通过。独立 Base-only 安装验证帮助/默认门禁/依赖缺失；OpenAI-only、Anthropic-only 安装各通过三个新 Smoke 场景，使用 `python -I` 与 MockTransport。
- 新增配置/报告 v1 Schema，历史 Agent/Provider Schema 与 Migration 不变；没有真实 API 调用或远程中间件变更。

以上为 b1 当时的离线快照；后续 b2 与真实验证结果见下节，不以历史“零真实调用”描述当前状态。

## 14. 0.4.3b2：响应计费元数据与尝试绑定

实现 [ADR 0020](adr/0020-observed-billing-context.md)，不升级 SDK，不引入新的存储系统或事件旁路。

- 新增 `ResponseBillingMetadata` 与 `ModelUsageObserved.billing`：服务等级、推理地域、5m/1h 缓存写入累计分项；与 Usage 在原事务内提交。
- 未观测保持未知，省略不清除事实；重复快照去重；标量漂移、计数回退、分项超过总量均拒绝，不发布失配语义。
- 两类实际 SDK 的开始/迟到字段映射，取消、断流和进程恢复保留已提交事实；不自动重发模型请求。
- 只有宿主明确声明匹配的 OpenAI/Anthropic 直连平台，才将对应原生事实映射为价格上下文；不把兼容协议当成计费平台，不自动确认代理/百炼计价规则或推理模式。
- 单一 TTL 必须由完整非零分项证明；混合/不完整分项不套单一费率。明确零写入不需要推断 TTL。价格绑定与后续估算均检查冲突。
- Agent Event/Thread **v5**、Provider Event **v3**；Migration 0005 只推进最低读者版本；原 v1–v4/v1–v2 Schema 冻结。CostReport / PriceSnapshot / UsageObservation v1 不变，成本报告不独立证明元数据来源。

验收（2026-09-03）：

- `make check`：**805 passed、1 skipped**（本地 PostgreSQL 未配置）；Ruff/Mypy 通过，72 个源文件。
- 异步调试 `tests/agent tests/models tests/smoke -W error`：**769 passed**。
- 相对 b1 新增 **69 项**：63 项领域/SDK/价格绑定测试、5 个硬崩溃切点、1 个真实 v4 迁移参数用例。
- 全项目 **54 个事务/进程硬崩溃切点**，另有 2 个 SIGINT 用例；本增量覆盖计费事实提交前后与双 SDK 元数据提交后，恢复不重发请求。
- v4 夹具由真实提交 `1e954ad2ad8387f1766c56ff590add4c154f802e` 生成；混合 v1–v4 历史升级/导出不改写；从该提交导出的实际旧读者拒绝 v5 数据库，返回 `schema_too_new`。
- 六个既有离线入口、sdist/wheel 构建通过。独立 Base-only/OpenAI-only/Anthropic-only wheel 安装验证默认门禁与两个 SDK 各三场景的 MockTransport Smoke。

## 15. 0.4.3c：百炼受控验证进行中

用户已确认北京地域并授权小预算测试。首次文本 Smoke 通过；首次工具场景在流解析处失败、工具执行数为 0，按停止条件未执行审批。额外一次经授权的结构定位确认后续分片空 ID 被误判为身份漂移；已转入兼容修复与离线回归，不将问题掩盖为供应商不可用或工具通过。

脱敏报告、调用范围、官方依据与后续状态见 [百炼验证记录](validation/bailian-2026-09-03.md)。本阶段未部署远程中间件；费用仍是 Token 估算能力，不是实测账单对账。

### 15.1 空 ID 增量兼容修复与真实复验

只修改 Chat 工具分片的 ID 判断：None/空字符串不声明新身份，其他非空 ID 必须与首个有效 ID 一致。工具名、类型、索引、参数 JSON、结束原因、Usage 校验不放宽，不拼接或猜测身份。

- 新增 6 项实际 SDK 离线回归：空 ID 下 null 参数/续传参数成功，最终无身份、非空 ID 漂移、空名称漂移、非法类型仍失败。先在旧代码复现 **2 failed、4 passed**，修改后全部通过。
- 当前 `make check`：**819 passed、1 skipped**，Ruff/Mypy 通过；异步调试 **783 passed**。54 个硬崩溃切点与 2 个 SIGINT 用例保留。
- 用户额外授权最多 4 次请求完成复验，工具与审批各 2 次，SDK/Adapter 均无重试；两场景均 passed、1 次内存工具执行、完整 Usage、内容与 Replay 一致，审批重开验证通过。
- 首次文本 1 次、首次工具失败 1 次、结构定位 1 次、复验 4 次，合计发起 **7 次**请求。失败/定位请求没有完整用量，因此不得将已知用量之和当作总账单。
- 已支持的三个固定功能场景通过；0.4.3c 尚余实际计价适用性与估算证据收口。未验证原生 OpenAI/Anthropic、其他百炼模型、Thinking 或真实编码能力；0.5 的最新状态见 [实施设计](m05-coding-tools.md)。

### 15.2 初始化竞态硬化

完整回归发现既有 SQLite 并发初始化的 WAL 切换偶发忙锁：最初全量为 810 passed、1 failed、1 skipped，不能以重跑碰巧通过交付。已通过真实 SQLite 竞争与旧提交故障注入确认根因，并按 [ADR 0021](adr/0021-session-wal-initialization.md) 修复。

迁移连接先关闭，仅在新连接中有界重试 WAL 切换的 SQLITE_BUSY；不重跑迁移或任何模型/工具，不放宽未知版本/外部数据库拒绝。新增 8 项存储测试，覆盖真实竞争、耗尽、取消清理、非忙锁错误与模式结果校验。加上空 ID 的 6 项，当前总量 **819 passed、1 skipped**，异步调试 **783 passed**。三个真实 Smoke 通过于 WAL 硬化前；硬化后不追加模型消费，使用相同 Kernel/SQLite 路径的离线回归验证。

### 15.3 CI 超时测试确定性收口

低速 Runner 暴露两个把短预算与“必然已进入请求/模型流”混为一谈的测试假设，运行时提前终止本身正确。已按 [测试规范第 13 节](testing-and-evals.md#13-ci-低速-runner-的超时测试边界2026-09-03) 将传输超时与 Kernel 期限到期拆开验证：前者在响应读取阶段注入 HTTP ReadTimeout，后者在明确检查点推进真实 asyncio.Timeout；同时覆盖进入 Provider 前已过期。未改变生产预算或追加 API 调用。当前全量 **820 passed、1 skipped**，异步调试 **784 passed**。

### 15.4 下一次计价验收准备

已形成 [ADR 0022](adr/0022-bailian-price-validation.md)：复用现有成本 API，不依据场景汇总补造逐次事实。新增求证明确百炼原生 service_tier 缺失/default 与 PTU 的区别，应用范围限于已核对的平台和固定模型；不能只凭归一化后的 None 推断原生字段缺失。

此前 7 次授权请求已执行，新的单次文本计价验证已询问、仍待回复；不重复消费旧授权。随后用户要求继续后续阶段，已独立推进 0.5.1 只读工具；计价证据保留未验收状态，不再因其阻塞无需模型调用的开发。
