# ADR 0050：模型可纠正的工具参数校验反馈

- 状态：已接受；0.5.5c3c实现、本地与跨平台离线验收完成，c3d真实验证待独立授权
- 日期：2026-09-06
- 范围：分页跨字段校验错误、模型可见反馈、持久历史、双Provider映射与兼容

## 1. 背景

任务v2真实Campaign的三个run都在`read_file`或`list_files`后续页遗漏`expected_revision`。严格Pydantic校验正确阻止不带观察身份的分页，但Runtime统一返回`tool_invalid_arguments / 工具参数不符合契约`。模型无法确定修正动作，连续重复调用，最终因增长的完整历史达到100000累计Token预算。

[源码研究](../research/tool-validation-feedback-applicability.md)表明Codex、OpenCode与Claude Code本地研究样本都把可恢复参数校验失败作为模型可见工具结果，而不是无条件终止Agent Loop。Harnessix需要同类可纠正性，但不能直接回显任意底层校验错误或参数值。

## 2. 决策

1. 保持`ReadFileInput`和`ListFilesInput`现有严格Schema及跨字段校验；
2. Runtime捕获`ValidationError`后，仅识别分页位置大于首页、`expected_revision`缺失或null、且补入固定占位revision后其余输入完整有效的情况；
3. 该情况返回`tool_expected_revision_required`和固定修正消息，其他参数错误继续返回`tool_invalid_arguments`；
4. 错误消息不包含调用参数值、路径、底层ValidationError、工作区内容或异常栈；
5. 失败仍持久化为普通`ToolResultContent`，由既有规范历史进入OpenAI-compatible与Anthropic wire；
6. 模型纠正必须提交新工具调用，不能把它实现为Provider自动重试或Runtime隐藏参数注入；
7. 不提高任务Token预算，不修改已完成Campaign，不在c3c发起真实Provider请求。

## 3. 错误契约

```json
{
  "outcome": "failed",
  "error": {
    "code": "tool_expected_revision_required",
    "message": "read_file后续页必须携带上一成功结果的revision作为expected_revision",
    "retryable": false,
    "category": "tool"
  }
}
```

`list_files`使用同一代码，消息中的工具名替换为`list_files`。工具名来自宿主固定绑定，不从模型参数读取。`retryable=false`表示同一无效输入不应由基础设施自动重试；Agent可基于反馈构造新的有效输入。

仅以下输入获得专用错误：

| 工具 | 位置条件 | revision条件 | 其他字段 |
|---|---|---|---|
| `read_file` | `start_line > 1`且为严格整数 | 缺失或null | 补入合法占位revision后完整通过原模型 |
| `list_files` | `offset > 0`且为严格整数 | 缺失或null | 补入合法占位revision后完整通过原模型 |

包含额外字段、缺失必填字段、类型错误、位置越界或非法非空revision时均回退通用错误。专用分类不得成为探测任意参数值的旁路。

## 4. 执行与恢复语义

```text
模型工具调用
  → 原输入模型校验失败
  → 窄范围安全分类
  → 失败ToolResult与原调用持久提交
  → Provider下一步收到规范失败历史
  → 模型提交新调用并显式携带上一成功revision
  → 原输入模型校验与正常执行
```

工具实现只在输入校验成功后执行。失败调用不读取文件系统、不创建Artifact、不触发审批或副作用。Session重开和Replay保留已提交错误，不重新分类、不执行原调用。模型新调用使用新的`call_id`，原失败不会被覆盖。

## 5. Provider映射

OpenAI-compatible路径继续使用`role=tool`，JSON正文包含`outcome/output/error/diff_artifact`规范子集。Anthropic路径继续使用`tool_result`并在失败时设置`is_error=true`。两者不得只传错误布尔值而丢失code/message，也不得创建Provider专用错误文案。

SDK `max_attempts`不受影响。模型看到失败后发生的下一次请求是Agent Loop新步骤；尝试账本和Usage按原规则累计。

## 6. 版本与兼容

本决策不修改输入/输出Pydantic模型、JSON Schema、工具风险与作用域、Agent Event v9、Provider Event v3、Session migration11、Action/Patch/Process/Artifact协议或数据库。工具定义指纹继续稳定，因为可执行参数、输出和副作用契约没有变化；变化仅是既有失败结果中更精确的公开code/message。

旧Session中的`tool_invalid_arguments`保持原字节和语义，重开不会升级历史错误。新版本处理尚未执行的旧调用时仍使用相同严格输入契约；若命中专用条件则生成新错误。调用方必须按错误代码而非中文消息分支。

## 7. 未采用方案

### 自动从历史注入revision

拒绝。多个文件或目录观察并存时会产生隐式选择，也会削弱分页乐观并发身份，隐藏模型调用错误。

### 回显完整Pydantic ValidationError

拒绝。错误结构随依赖版本变化，且可能包含参数值、路径或未来的敏感字段，不适合作为稳定公开契约。

### 为所有校验错误立即建立通用详细分类

拒绝。本阶段只有真实证据支持分页revision缺失。批量扩大错误面会增加泄漏和兼容风险；统一Tool Error Taxonomy仍按路线图独立推进。

### 再次提高任务预算

拒绝。真实数据表明重复无效调用和完整历史增长才是因果链；更高预算只增加费用。

## 8. 验收与下一阶段

c3c必须覆盖：

- `read_file`/`list_files`缺少与显式null revision的专用错误；
- 复合无效输入和其他校验错误继续通用失败；
- code自动归类为`tool`且消息不回显参数canary；
- 失败结果持久化、SQLite重开和Replay一致；
- OpenAI-compatible与Anthropic映射均保留code/message/category，Anthropic设置`is_error=true`；
- 确定性Agent循环在一次专用错误后复制首个成功结果的revision，后续页成功并完成Turn；
- 全量质量门禁、异步严格回归、Schema无漂移和隔离wheel验证。

c3c只能证明运行时具备纠正协议，不能证明真实模型一定采用反馈。c3d必须使用新Campaign ID、独立run和单独费用授权验证真实纠正率；未经授权不发起网络请求。
