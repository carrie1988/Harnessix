---
doc_type: adr
status: current
version: 2
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
  - tests/app_server/test_rejection_sdk_chain.py
  - tests/protocol/test_rejection_protocol_v2.py
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
   - 不新增事件版本或执行路径。内部事件早已支持该拒绝原因；公共协议v2的拒绝原因同步放行
     `invalid_arguments_json`（v2修订）。原先只允许`unregistered_tool`，服务端投影这类拒绝时校验失败、
     客户端退出并取消回合，holdout中实测2次。
   - 产品界面按原因显示：“工具未注册”或“工具参数不是合法JSON”。
2. **主动压缩失败不终止回合。**
   - 摘要截断改用独立代码`provider_summary_truncated`；用量照常记账，窗口不激活。
   - 规划阶段判定无法压缩（`context_compaction_retained_overflow`、`no_progress`、`source_overflow`）时，
     尚未写入任何压缩记录（v2修订）。预算压力路径原本就把这三种视为“本次不压缩”，阈值触发路径与之对齐。
     holdout中实测：近期两组并行读取的结果超过保留预算，阈值触发规划失败而终止，当时实际输入仅32–41K token。
   - 主动压缩（历史阈值或预算压力触发）遇到以上任一失败时，本步保留原历史继续。
   - 该Turn历史UTF-8字节再增长1/4之前，不再规划压缩，以免同一步重复规划。
   - 退避状态只用于调度，不进入事件；进程重启后最多多一次摘要请求。
   - Context Overflow触发的被动压缩仍终止。
   - 产品摘要上限保持`min(max_output_tokens, 2048)`（v2修订，撤回v1的4096）。
     摘要字节预留为12288，输出上限按每Token 4字节从预留换算；中文摘要每Token常有3–6字节，
     上限升到4096后摘要会在流式输出中途超出预留（`context_compaction_summary_overflow`，holdout中实测1次）。
     中途超限时该次用量不完整，按账本规则不能再发下一次请求，因此不能像截断一样跳过。
     恢复2048后，最坏情况是摘要截断，而截断用量完整，可以按上条跳过。
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
