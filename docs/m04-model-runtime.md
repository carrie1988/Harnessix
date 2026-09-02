# 0.4 Model Runtime 实施计划

- 日期：2026-09-03
- 状态：0.4.1 与 0.4.2a 已完成离线验收；0.4.2b/0.4.3 待实施，真实平台未验证
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

分为两个可验收切片，见 [ADR 0015](adr/0015-anthropic-provider.md)：

- **0.4.2a 已完成**：Anthropic Messages Adapter、HTTPX2 类型隔离、共享 Provider 契约、缓存计数纳入输入总量、跨 Provider 继续与审批重启；
- **0.4.2b 待实施**：Model Attempt 身份、完整/部分/未知用量、缓存/推理明细、失败 Usage 和版本化持久记录。不把本次双 Adapter 通过写成 0.4.2 全部完成。

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

主路线图中 0.4 的 Provider、能力、认证、错误/重试、取消、Token/成本和脱敏诊断均有实现与测试证据；两类 Adapter 共享契约通过，至少完成受控真实平台验证后再进入 0.5 Coding Tool Runtime。

当前不需要安装远程中间件。尚未选定真实测试模型或价格基线；凭据只由运行环境注入，默认测试无需 Key。

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
| 请求输出预算 | min(剩余 Turn Token、配置输出上限)；不能精确预扣输入 Token |
| 认证与错误 | Secret 环境引用；闭集 code/retryable，不输出原始异常 |
| 取消与超时 | 建连、读流、错误 body、退避和调用方 Task；响应关闭 |
| Reasoning / 多模态 / 内置工具 | 未开放；不透传私有推理，公开摘要 Item 暂不接受 |
| 缓存/推理 Token、价格、失败请求费用 | 待 0.4.2/0.4.3；不能把当前计数当作供应商账单 |
| Anthropic | 0.4.2a 已通过离线验收，具体严格配置见下节 |
| 真实平台 Smoke | 未完成，不以离线测试替代 |

流式成功要求 finish reason、Usage 和 `[DONE]`，不支持缺失流式 Usage 的兼容服务。SDK 在终结符后停止读取；同一已读块中的额外终结数据会拒绝，不承诺读取终结符后所有网络字节。

## 7. 离线验收与下一切片

- `tests/contracts/provider.py`：供应商中立的 11 条共享行为契约，目前由 OpenAI Adapter 工厂实例化；
- `tests/models/`：101 条测试，包含真实 SDK、MockTransport、坏 JSON/尾包、资源边界、HTTP 错误 body 清理、认证隔离和 Kernel 多步骤/审批/Replay；
- `examples/kernel_openai_offline.py`：真实 SDK→Provider→Kernel→SQLite 的可运行离线入口，不调用真实 API；
- `make check`：280 passed、1 skipped（本地未配置 PostgreSQL）；Ruff 与 Mypy 通过；
- `PYTHONASYNCIODEBUG=1 uv run pytest tests/models tests/agent -W error`：244 passed；
- 四个 Kernel 离线入口、sdist/wheel 构建均通过；独立 Python 3.12 wheel 环境验证基础 Kernel 无 SDK 依赖，安装可选 SDK 后端到端离线验收通过；
- 旧 Agent Event/Thread/Provider Event Schema 未改变，新增配置 Schema 纳入代码一致性测试。

实现时复现并修复了 SDK 在“HTTP 错误响应尚未返回 AsyncStream、读取 body 失败或取消”路径的资源清理缺口：关闭责任下沉到有界 Transport，不只依赖 Adapter 的 `finally`。回归同时覆盖正常流和错误 body。

上述数字为 0.4.1 收口快照，0.4.2a 最新结果如下。

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
- 累计 Usage 更新取最后值，不逐块相加；输入总量包含两个缓存计数；子项明细尚未持久化；
- context_overflow 和中途异常当前仅保留失败分类，失败用量完整性将在 0.4.2b 处理；
- 配置 Schema 为 `spec/anthropic-config-v1.schema.json`；OpenAI 配置 Schema 语义保持相同（仅字段排列变化），历史事件 Schema 和 Migration 不变。

验收结果：

- `tests/models/`：219 passed，其中原有 101 项保留，新增 118 项；两类 Adapter 分别实例化同一组 11 条核心契约；
- `make check`：398 passed、1 skipped（本地未配置 PostgreSQL）；Ruff/Mypy 通过；
- 异步调试下 Kernel + Provider：362 passed，警告作为错误处理；
- 验证包含 Anthropic→OpenAI 会话切换、审批暂停后换 Provider 继续、Usage/配对/Replay、SDK 隐式类型转换、错误 body 关闭与单网络块 Ping 上限；
- 五个离线入口、sdist/wheel 构建通过；独立 Python 3.12 环境验证基础包无模型 SDK/HTTPX2 依赖，Anthropic-only 与 OpenAI-only 两种安装分别完成 SDK→Kernel→SQLite 闭环；
- 真实 Anthropic/百炼 API 调用：0，仍待受控验证。

下一切片按 ADR 0015 的 0.4.2b 方案实施用量事实与跨版本迁移。真实验证需要宿主中可读取的凭据、地域和模型配置；这一外部条件不阻塞离线开发。
