# ADR 0019：受控模型 Smoke 与白名单诊断

- 日期：2026-09-03
- 状态：设计采纳；实现与离线验收见 `docs/m04-model-runtime.md`
- 对应阶段：0.4.3b1；不代表 0.4 或生产级 Coding Agent 已完成

## 背景与决策

复用已有 Adapter、Kernel、审批和 SQLite，不另建 Agent Loop。新增 `harnessix model-smoke`，只验证固定的小型协议闭环，不允许用户 Prompt、工作区读取、Shell、文件修改或任意工具。SDK 保持可选依赖。

1. CLI 与库入口均默认不执行，必须显式 `allow_network=True` / `--allow-network`；门禁先于 Provider 工厂、凭据读取与网络调用。配置只接受环境变量名称，不接受 Key 值，不探测端点或模型。
2. 三个场景：`text` 精确输出固定标记；`tool` 调用一次内存标记读取工具后返回本次随机标记；`approval` 同一工具先持久暂停，关闭/重开 Kernel，按原指纹批准并继续。选择 approval 即授权**这一个内存夹具**的自动审批，不扩展到业务审批。
3. 默认输出上限 128 Token、时间 30 秒；最多 512 输出 Token/请求、60 秒 Turn、2 个模型步骤、1 个工具调用/步骤；两个 SDK 与 Adapter 均不重试。输入 Token 无法精确预扣，Token 检查不是供应商实际消费或金额硬上限。
4. 每次使用独立 0700 临时目录与 0600 Session，正常退出清理；关闭 Kernel 后重开数据库，再比较持久快照与事件 Replay。不是操作系统进程崩溃测试，也不承诺崩溃后无残留或安全擦除。
5. JSON 报告只含枚举、计数和检查结果，不含 URL、模型字符串、响应 ID、Prompt、标记、工具参数、响应正文、Header、原始异常。完整用量检查失败时不得判定 Smoke 通过；已知消费是下界，尝试数是 Started 事实，不是网络计费请求数。异常导致账本不可读取时用 null，不编造 0。
6. 默认 SDK 工厂与注入工厂分别标记 `sdk_default` / `injected`；注入并不天然保证离线，测试必须另以 MockTransport 保证。任何注入结果不能伪装成真实平台验收。
7. CLI 错误不回显参数/配置内容，运行期间禁用 Python logging 后恢复；库不修改宿主日志策略。报告白名单不是 Session 通用 DLP：恶意供应商若在语义内容中反射敏感信息，私有 Session 仍可能包含该内容；不导出 Session 或任意 Trace。
8. 库级 Task 取消继续向上传播，先由 Kernel/SDK 完成清理；CLI Ctrl-C 输出固定取消报告，消费计数未知。CLI 不读取 Action Plane 的环境配置。

## 不纳入本切片

计费上下文自动采集拆为 **0.4.3b2**：已核对本地锁定 SDK 的响应类型，OpenAI 响应 `service_tier` 与 Anthropic Usage 的 `service_tier` / `inference_geo` / 缓存 TTL 明细需要独立映射、持久身份与兼容性设计，不能根据请求值推断实际值。

- [OpenAI Chat 响应契约](https://developers.openai.com/api/reference/ruby/resources/chat/subresources/completions/methods/retrieve)：响应中的实际处理等级可能与请求不同；这里只引用协议事实，Python 实现以锁定 SDK 源码为准。
- [Anthropic Service tiers](https://platform.claude.com/docs/en/api/service-tiers)：请求选择与响应实际等级不同。
- [百炼 OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)：真实测试前仍需核对目标地域、模型及流式工具能力；本切片不默认选定模型。

0.4.3c 再执行真实平台调用；价格未知保持未知，不在 Smoke 报告内附加虚构费用。本切片不需要新中间件。

## 验收标准

- 两种真实 SDK × 三场景，以各自 HTTP 库的 MockTransport 完成 Kernel/SQLite/重启/Replay。
- 默认禁用、配置严格边界、超时、取消、无重试、错误正文与恶意字段隔离、预算、错误工具/参数/标记、缺失 Usage、资源关闭。
- CLI 参数/配置错误不回显 canary；基础安装可查看帮助与拒绝未授权执行，无 SDK 顶层依赖。
- 配置/报告各有独立 v1 JSON Schema；历史事件和迁移不变；全量回归、构建及可选安装检查。
