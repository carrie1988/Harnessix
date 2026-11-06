---
doc_type: adr
status: current
version: 1
code_revision: 01e5e888e49e7a39fc1ac191b690f5d3b84e41c5
owners: [core]
modules: [agent, models, context, tools, product_config]
related_adrs:
  - docs/adr/0008-provider-event-model.md
  - docs/adr/0023-workspace-read-tools.md
  - docs/adr/0061-terminal-turn-retry-and-provider-neutral-history.md
related_tests:
  - tests/models/test_openai_chat.py
  - tests/context/test_compaction_runtime.py
  - tests/agent/test_pressure_compaction_loop.py
  - tests/tools/test_read_error_guidance.py
  - tests/product_config/test_thinking_config.py
supersedes: []
---

# ADR-0109：单次坏输出可恢复与读工具失败引导

## 1. 背景与问题

week-1本地模型holdout中期分析（Verified A2x4前41次尝试）显示：7次尝试（17%）因单次坏输出而整题终止，
不是预算耗尽：
- 5次是输出用满4096 token、工具参数JSON被截断；
- 2次是压缩摘要用满2048 token。

另有19%的模型步骤所有工具调用都失败，耗去18%的输入token。主要原因有三：
- 把命令输出存档编号当作`read_file`路径；
- 读工具用了命令容器内路径`/testbed/...`；
- 读工具的5秒时限在负载下超时。

## 2. 决策

1. **截断的工具调用按目录拒绝处理。**
   - 产品`ProviderDefinition.repair_tool_arguments`映射到Model层同名开关，回环自托管预设开启。
   - 开启后，修复仍非法的参数（包括截断）只形成`invalid_arguments_json`拒绝，回合按已有的v21目录拒绝重入继续。
   - 以`length`结束但已有流式工具调用时，按同一规则形成调用组，终态记为`tool_calls`。
   - 纯文本截断仍以`max_output_tokens`失败。
   - 这样不新增事件版本、拒绝原因或执行路径。
2. **主动压缩的摘要截断不终止回合。**
   - 摘要截断改用独立代码`provider_summary_truncated`；用量照常记账，窗口不激活。
   - 主动压缩（历史阈值或预算压力触发）遇到它时，本步保留原历史继续。
   - 该Turn历史UTF-8字节再增长1/4之前，不再规划压缩，以免同一步重复规划。
   - 退避状态只用于调度，不进入事件；进程重启后最多多一次摘要请求。
   - Context Overflow触发的被动压缩仍终止。
   - 产品摘要上限改为`min(max_output_tokens, 4096)`。
3. **读工具失败给出可操作说明，并映射容器路径。**
   - `path_denied`、`not_found`、`timeout`返回下一步建议。
   - `read_file`误用存档时，提示改用`read_artifact`。
   - 宿主声明的命令容器工作区挂载点前缀，换算为相对路径后再走原校验。
   - 读时限可配置（1～60秒）：默认5秒，描述符字节不变；非默认值进入描述符规则与版本。
   - 产品配置`workspace_read_timeout_seconds`，回环自托管预设20秒。
4. **通用执行指令升为v3。**
   - 独立的只读查询在一步内并行发出；
   - 读工具使用相对路径；
   - 单次输出保持精简：用局部编辑代替整文件替换，长脚本分步写入。

## 3. 后果与边界

- **不新增可执行能力，不扩大可读范围。**
  - 拒绝的调用从不执行；
  - 路径换算之后仍经过原有的`..`、绝对路径和敏感路径校验。
- **审计可见。** Call Item保留模型的原参数，结果路径是换算后的相对路径。
- **截断后的重试由模型完成，不由运行时自动重放；每次重试消耗原有的步数和token预算。**
  拒绝结果是固定文本，不单独说明“输出过长”，这一点由v3指令补充。
- **未覆盖的情况：**
  - 纯文本截断；
  - 截断导致的调用结构损坏（缺少ID或名称）。

  这两类仍按原有规则失败，可能需要新的事件版本时再另行决策。
