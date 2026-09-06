# Coding Tool参数校验反馈适用性源码研究

- 日期：2026-09-06
- Harnessix问题：任务v2真实Campaign的三个run均在分页调用遗漏`expected_revision`后连续重试并耗尽累计Token
- 固定参考提交：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`、Claude Code本地研究样本 `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`

## 1. 待求证问题

严格工具输入契约已经正确阻止缺少乐观并发身份的后续页读取，但通用错误不能让模型纠正调用。实施前需要回答：

1. 主流Coding Agent是否把可恢复的工具参数错误返回模型，还是直接终止Agent Turn；
2. 是否应把底层校验器的完整错误、参数路径和值直接暴露给模型；
3. Harnessix应自动补充`expected_revision`，还是要求模型显式携带；
4. 错误如何进入持久Session、双Provider wire和Replay，又不改变既有工具输入Schema与副作用边界。

## 2. Codex源码事实

`codex-rs/tools/src/function_call_error.rs:3-9`把模型可见工具失败分为`RespondToModel(String)`与`Fatal(String)`。前者保留Agent循环，后者才是运行时致命失败。

`codex-rs/core/src/tools/handlers/mod.rs:83-89`把JSON反序列化失败转换为`RespondToModel`，并提供“参数解析失败”的具体原因。同文件的跨参数规则也使用面向模型的修正文本，例如`justification`要求显式`sandbox_permissions`。这证明参数校验失败不是必须终止Turn的内部异常，且跨字段要求应给出可执行修正。

Codex示例会包含底层反序列化错误。Harnessix不能直接照搬这一披露范围：工具参数可能含路径、查询、补丁或未来的敏感引用，底层异常格式也不是稳定公开契约。

## 3. OpenCode源码事实

`packages/opencode/src/tool/tool.ts:18-33`定义可匹配的`InvalidArgumentsError`，消息明确说明工具、校验详情并要求模型重写输入。`tool.ts:99-147`在工具初始化时编译参数Decoder，在实际执行前解析输入；失败被转换为上述类型，成功后才进入工具实现。

工具可用`formatValidationError`定制详情，否则回退到通用错误字符串。这表明错误类型、模型反馈和工具执行边界是三件独立的事：返回可纠正错误不代表放宽Schema，也不等于Provider自动重试。

Harnessix采用相同的“执行前严格校验、失败作为工具结果返回”方向，但不采用任意`String(error)`回显，以避免依赖库版本漂移和输入值泄漏。

## 4. Claude Code本地研究样本事实

`src/services/tools/toolExecution.ts:614-675`在调用工具前执行Zod校验；失败时构造`tool_result`，设置`is_error: true`，并以`tool_use_error`内容返回模型，而不是调用工具实现。

`src/utils/toolErrors.ts:43-131`把校验问题归纳为缺失参数、意外参数和类型不匹配，生成更适合模型理解的文本。其格式化有字段路径和类型信息，并设有日志截断边界。该实现进一步证明模型需要可操作反馈，但其通用字段回显策略不必成为Harnessix的安全边界。

该样本只作为本地架构研究输入，不作为上游官方接口或兼容承诺。

## 5. Harnessix真实证据与现状

[任务v2真实基线](../validation/bailian-2026-09-06-coding-eval-v2/README.md)固定三个独立run。模型均成功执行初始测试和至少一个读取步骤，随后出现以下共同序列：

```text
read_file首页成功，结果含revision与next_line
  → read_file(start_line > 1, expected_revision缺失)
  → Pydantic跨字段校验失败
  → tool_invalid_arguments / 工具参数不符合契约
  → 模型以近似参数重复调用
  → 完整历史增长
  → 100000累计Token预算终止
```

三个run分别至少连续出现3、4和8次同类无效调用。终态主分类是`budget`，但可操作因果点是模型无法从通用错误得知分页必须携带哪个稳定身份。继续提高预算只会增加重复请求成本。

Harnessix已有以下正确基础：

- `ReadFileInput`与`ListFilesInput`在执行前严格拒绝缺少revision的后续页；
- `ToolResultContent.error`持久化规范`AgentFailure`，失败结果可以Replay；
- OpenAI-compatible映射把完整规范工具结果写为`role=tool`；
- Anthropic映射把相同结果写为`tool_result`并根据outcome设置`is_error`；
- Provider重试与Agent新模型步骤相互独立，Campaign固定`max_attempts=1`。

缺口只在错误内容：Runtime捕获全部`ValidationError`后统一返回`tool_invalid_arguments`。

## 6. 设计输入

### 6.1 保留显式revision，不自动注入

`expected_revision`是模型对上一观察版本的显式声明，也是目录/文件分页的乐观并发身份。Runtime若从历史自动选择某个revision，会隐藏模型参数错误，并在多个读取结果存在时引入隐式选择。后续页继续必须由模型明确复制上一成功结果的`revision`。

### 6.2 只识别已知且唯一的跨字段错误

仅当下列条件同时成立时返回新错误：

- 工具是`read_file`且`start_line > 1`，或工具是`list_files`且`offset > 0`；
- `expected_revision`缺失或为null；
- 使用固定占位revision重新校验后，其他所有字段均符合原输入模型。

如果还存在缺少`path`、额外字段、错误类型、越界数值或非法revision，则继续返回通用`tool_invalid_arguments`。这样不会把某一个修正误报成调用的唯一问题。

### 6.3 固定公开内容

新失败契约为：

| 字段 | 值 |
|---|---|
| code | `tool_expected_revision_required` |
| category | `tool`（由既有前缀分类） |
| retryable | `false`（不是同一输入的传输重试） |
| message | `<tool>后续页必须携带上一成功结果的revision作为expected_revision` |

消息只包含受信绑定表中的固定工具名和固定字段名，不包含参数值、文件路径、Pydantic错误、调用栈或工作区内容。其他校验错误保持原通用代码和消息。

### 6.4 持久化、Provider与恢复

错误仍是普通失败`ToolResultContent`，与调用同一Turn持久提交；重开和Replay读取原事实，不重新执行工具或重新格式化历史错误。双Provider映射复用既有规范历史，不增加供应商专用分支。

模型收到错误后发起的新调用是新的Agent步骤和新工具调用身份，不是SDK自动重试。原调用保持失败，便于审计连续错误与Token成本。

## 7. 结论

0.5.5c3c应实施一个窄范围行为修正：保留输入Schema和分页并发要求，只为“缺少revision且其他参数有效”返回稳定、有界、模型可纠正的工具错误。直接工具、通用回退、分类、Session持久化、Replay、OpenAI-compatible wire、Anthropic wire和完整Agent纠正循环均须离线回归。

该实现不需要Agent/Provider/Session/数据库Schema升级，也不改变工具定义指纹；输入、输出、风险、作用域和副作用语义均未变化。真实模型是否能稳定纠正仍必须由新Campaign验证，不能用确定性Provider测试替代。
