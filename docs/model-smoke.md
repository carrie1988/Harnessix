# 受控模型 Smoke 使用说明

本入口验证 **SDK → Adapter → Kernel → SQLite → Replay** 的固定闭环，不是交互式 Coding Agent，不读取业务仓库或执行 Shell。当前离线验证通过；真实平台兼容性仍待 0.4.3c 验收。

## 1. 默认不开启网络

~~~bash
uv run harnessix model-smoke --help
uv run harnessix model-smoke --config .harnessix/smoke.json
~~~

第二条命令输出 `network_not_enabled`、退出码 2，连配置文件都不会读取。`--allow-network` 只授权本次固定场景，不保存永久许可。默认 CI 使用实际 SDK 与 MockTransport，没有平台调用。

## 2. 配置与执行

先按平台官方文档核对模型 ID、地域/端点、流式工具能力、输出 Token 参数及价格，再在被 Git 忽略的 `.harnessix/smoke.json` 创建配置。以下 URL/模型均为占位，不能原样用于真实调用：

~~~json
{
  "provider": "openai_chat",
  "base_url": "https://provider.invalid/v1",
  "model": "replace-with-verified-model-id",
  "api_key_env": "DASHSCOPE_API_KEY",
  "scenario": "text",
  "max_output_tokens": 128,
  "max_tokens": 2048,
  "timeout_seconds": 30,
  "output_token_parameter": "max_tokens"
}
~~~

- 凭据从已配置的环境变量读取；JSON 只填变量名，不填 Key。入口不自动读取 `.env`。
- OpenAI-compatible 并非某一个计费平台；`output_token_parameter` 缺省为 `max_completion_tokens`，兼容平台是否需要 `max_tokens` 必须自行求证。
- Anthropic 使用 `provider=anthropic`，明确配置已核对的 Messages 端点/模型/凭据变量，并**删除** `output_token_parameter`。现有 Adapter 仅支持非 Thinking 配置。
- 不接受自定义 Prompt、工具、重试次数、Header、代理、计费上下文或模型参数透传；配置额外字段、重复 JSON key、非有限值、非普通文件、超过 16 KiB 的文件均拒绝。
- `OPENAI_CUSTOM_HEADERS` / `ANTHROPIC_CUSTOM_HEADERS` 非空时配置失败，避免环境认证污染。

选择对应 extra，然后显式启用：

~~~bash
uv run --extra openai harnessix model-smoke --config .harnessix/smoke.json --allow-network
# Anthropic 配置使用 --extra anthropic；不要以修改 provider 一项代替核对整个配置。
~~~

| 场景 | 验证内容 | 模型步骤上限 |
| --- | --- | --- |
| `text` | 精确输出固定文本标记、完整用量、持久化与 Replay | 1 |
| `tool` | 调用一次内存读取工具，随后精确返回本次随机标记 | 2 |
| `approval` | 同一内存工具先暂停，关闭/重开 Kernel 后按原指纹批准，再继续 | 2 |

选择 `approval` 并启用网络，即授权测试宿主自动批准**这一个内存夹具**；不是批准真实读写工具，也不是客户端审批 UI。Kernel 重开与数据库重新加载不是操作系统进程崩溃验收。

## 3. 预算与诊断语义

- SDK 和 Adapter 均不重试。单步最多 1 个工具调用；工具拒绝第二次读取。
- 每请求默认最多 128 输出 Token、可配置上限 512；Turn 默认 30 秒、可配置上限 60 秒；默认 Token 检查阈值 2048、最高 8192。输入 Token 无法精确预扣，不能把检查阈值当作消费/金额硬上限。请求最多 64 KiB，响应最多 512 KiB，单帧最多 64 KiB，最多 2048 块。
- `attempts_started` 是持久化的尝试意图，不是已收费请求数；`known_*_tokens` 是已知消费下界，同时看 `usage_complete`。异常无法取得账本时为 null，不补零。
- `reason=passed` 才表示本场景全部检查通过；Turn 完成但没读工具、返回错误标记、缺少完整账本或 Replay 不一致都不能通过。
- `execution=sdk_default` 说明使用默认 SDK 工厂，不证明平台已收费；`injected` 说明使用宿主注入工厂，既不能宣称是真实平台验证，也不能仅凭此字段断言离线。测试以 MockTransport 保证离线。
- 报告仅有枚举、计数、布尔值，不包含端点、模型字符串、响应 ID、Prompt、标记、参数、Header、响应/异常原文。`provider_failure` 是现有闭集错误契约；未知错误不回显字符串。
- 报告不计算费用，也不自动确认地域、服务等级、推理模式、TTL。未知计费上下文不会套用默认价格。

退出码：0 通过；1 已执行但失败/缺依赖/缺凭据等；2 参数或配置文件无效、或没有显式启用；130 Ctrl-C。参数错误只输出固定 stderr，其他诊断为 stdout JSON。

## 4. 数据与资源边界

每次使用独立 0700 临时目录和 0600 Session，正常退出后清理，不默认导出会话。进程强制终止可能留下私有临时数据，清理不等于安全擦除。

CLI 在执行时禁用标准 Python logging，结束后恢复；库入口不更改宿主日志策略。报告白名单不等于通用 Session DLP：若不可信服务在语义内容中反射敏感字符串，私有 Session 可能保留该内容；不要将任意 Session/Trace 当作脱敏报告发布。

Task 取消向库调用者传播，SDK/Kernel 先关闭资源；CLI Ctrl-C 的报告不编造调用次数或费用。异常报告没有原始堆栈，详细排查应在离线夹具中复现，而不是开启凭据/正文日志。

## 5. 库入口与默认离线验收

~~~python
from harnessix.smoke.contracts import SmokeConfig
from harnessix.smoke.runner import run_smoke

# config 是经过核对的 SmokeConfig；默认调用只返回禁用状态。
# report = await run_smoke(config)
# 明确授权后：
# report = await run_smoke(config, allow_network=True)
~~~

测试宿主可注入异步上下文工厂 `provider_factory(config)`，其上下文提供 `ModelProvider`。该接口用于复用现有真实 SDK 的离线传输测试，不是 CLI 插件加载入口。配置与报告 Schema 位于 `spec/model-smoke-*-v1.schema.json`。

~~~bash
uv sync --locked --all-extras --dev
uv run pytest tests/smoke
PYTHONASYNCIODEBUG=1 uv run pytest tests/agent tests/models tests/smoke -W error
~~~

完整设计见 [ADR 0019](adr/0019-controlled-model-smoke.md)；当前进度与后续计费上下文/真实平台验收见 [0.4 实施计划](m04-model-runtime.md)。
