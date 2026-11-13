---
doc_type: adr
status: reviewing
version: 2
code_revision: pending
owners: [core]
modules: [product_config, models, product_ui]
related_adrs:
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
related_tests:
  - tests/product_config/test_thinking_config.py
  - tests/models/test_chat_mapping.py
  - tests/product_config/test_default_thinking_effort.py
supersedes: []
---

# ADR 0146 思考强度档位

- 状态：评审中
- 日期：2026-11-12

## 背景

产品负责人2026-11-12用本机安装的0.10.0b20试用（百炼qwen3.8-flash）：让Agent创建一个演示黑洞的3D网页。

- 第一次模型请求运行了10分钟：单次输出上限32,768 Token，其中28,502个用在思考上，剩下的额度写不完文件，
  写入调用在上限处被截断、被拒绝。
- 整个任务两个多小时仍未结束：四轮共115次请求，约29.7万输出Token里约21.3万（72%）是思考。

查证：

- 百炼文档：qwen3.8-max的`reasoning_effort`有low、medium、xhigh三档，默认xhigh；`thinking_budget`到达后
  模型停止思考并作答；`max_tokens`只限制正文，`max_completion_tokens`才限制思考加正文。
- 直接调用接口实测（同一个简单网页任务）：qwen3.8-flash默认思考17,378 Token、用时405秒；`reasoning_effort`
  为low、medium时思考136、175 Token，用时42、50秒；`thinking_budget=4000`时思考171 Token。qwen3.8-max的
  low、medium为239、119 Token。qwen3.8-flash接受`reasoning_effort`（文档未写）。
- 真实终端界面里用负责人的原任务各跑一次：默认（无上限）60分钟被时限终止；关闭思考20分钟未完成；
  思考上限1K、档位low时模型思考被掐断，只回复了一句计划就结束；上限2K、8K 20分钟未完成；
  **档位medium 8.7分钟完成**，是八种设置里唯一完成的。
- 主流Coding Agent（Claude Code、Codex CLI、Qwen Code）给用户的都是强度档位而不是Token数，默认中档；
  Token预算只作为个别模型的映射或高级选项。

## 决策

1. **给用户的是思考强度档位：低、中、高。** 模型按档位自己规划思考的长短，而不是想到一半被掐断。
   产品配置`ModelProfile.thinking_effort`（`low`、`medium`、`high`，可不设）。
2. **核实过档位的模型默认中档。** 向导（`effort_tiers_verified`）对百炼qwen3.8-max、qwen3.8-flash在开启思考时
   写入`medium`。未核实的端点与模型不写入，行为不变。
3. **按思考协议映射。** `dashscope_enable_thinking`：low、medium随开启思考的请求发送`reasoning_effort`；
   high不发送（这两个型号不发送时就是服务端默认的最强档）。其他协议目前没有档位映射，只有开和关。
4. **思考Token上限是显式的高级选项。** `ModelProfile.thinking_budget_tokens`（256起）：设置后发送
   `thinking_budget`，不再发送档位（qwen3.8-max不接受两者同时出现）。默认不设硬上限。
5. **到不了模型的设置在configure时拒绝。** `--thinking-effort`、`--thinking-budget`只对核实过的服务和模型、
   且没有关闭思考时接受；两者同时给出也拒绝。合同层同样校验：需要支持的思考协议、不能关闭思考、二者只能设一个、
   上限必须低于单次输出上限。
6. **放开思考预算对输出参数的限制。** `OpenAIChatConfig.thinking_budget`原先要求`max_completion_tokens`；
   百炼预设用`max_tokens`（只限制正文），思考上限在这种参数下同样有效，校验不再限制。
7. **已有配置运行时按中档（第2版修订）。** 配置文件不改；运行时对核实过档位的模型，没有设置强度与上限、也没有
   关闭思考的配置按新配置的写法补上思考协议与`medium`（`product_config/default_thinking.py`）。第1版只在启动时
   提示重新运行configure，负责人本机的配置（11月10日生成）一直没有改，一个任务的第一步想了11分23秒、用满32,768个
   输出Token仍没有写出内容；负责人2026-11-13决定运行时自动处理，原来的提示随之删除。

## 没有做的

- 没有为DeepSeek官方、自托管端点、强制思考的模型提供档位：没有核实过可用的参数。
- 没有宿主侧的“思考超量即中止”：会浪费已思考的部分，先观察档位的效果。
- 没有会话内切换档位的快捷方式。
- 小窗口（低于64K）默认关闭思考的规则没有变；中档下思考量很小，这条规则是否还需要另行评估。

## 后果

- 新生成的百炼qwen3.8配置思考量大幅下降，单步耗时从分钟级降到秒级到几十秒；复杂推理的深度相应降低，
  需要时可以用`--thinking-effort high`恢复原来的行为。
- 公开Schema新增三个可选字段（`reasoning_effort`、`thinking_effort`、`thinking_budget_tokens`），已有数据不受影响。

## 修订（第2版，2026-11-13）

- 决策第7条改为运行时补齐：`with_default_thinking_effort`在创建OpenAI Chat适配器前判断，条件是活动模型核实过档位
  （`effort_tiers_verified`）、不是自托管端点、Profile既没有强度也没有思考上限且没有把思考设为`off`、
  Provider没有思考协议或协议与预设一致。满足时补上预设的思考协议（及是否省略`tool_choice`）与`medium`，
  与新配置发出的请求相同；配置文件不变。
- 0.10.0b19之前的配置没有思考协议，原来每个请求都按服务端默认的最强档思考；补齐后改为只在工具步骤以中档思考，
  思考量只减不增。窗口小于64K的旧配置同样如此（新向导对这类窗口写入`off`，旧配置没有这个字段）。
- 启动提示（空会话与`doctor`里的“重新运行configure”）已不成立，与它专用的显示通道一起删除。
