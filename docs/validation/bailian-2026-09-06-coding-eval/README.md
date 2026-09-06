# 百炼北京三次Coding Eval基线验证

- 验证日期：2026-09-06
- Harnessix实现提交：`bbfd446707acbd9f945657ad96a6556d24af5df5`
- CI：[34034578492](https://github.com/carrie1988/Harnessix/actions/runs/34034578492)，四项任务全部通过
- Campaign ID：`765d1c97-75fe-4e37-baa3-8db4950ad57b`
- 任务：`harnessix-openai-empty-incremental-call-id/v1`
- Provider：百炼华北2（北京）OpenAI兼容Chat Completions
- 精确模型：`qwen3-coder-plus-2025-09-23`
- 隔离：`private-managed-copy-no-os-sandbox`

## 1. 授权与固定边界

本次只执行三个有序、相互独立的run。每个模型步骤最多一次Provider尝试，`max_attempts=1`、重试延迟为零；工具调用开启且禁止并行工具调用。单次最大输出4096 Token，任务总预算为16个模型步骤、20000个累计报告Token和600秒。

Campaign使用人民币10元累计已知费用停止线，并在每个完整试验之后核对。该门禁不是云账户硬额度，不能在已开始请求内实时停止。价格快照固定为北京按量付费、单次输入不超过32K时输入每百万Token 4元、输出每百万Token 16元，来源见[百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)。

凭据只由私有进程环境注入。配置、计划、状态、报告、命令行、仓库和本记录均不保存API Key值、Authorization Header或供应商响应正文。共享北京兼容端点的地域边界见[接入域名说明](https://help.aliyun.com/zh/model-studio/beijing-access-information)。

## 2. 结果

| Run | 主分类 | 模型步骤/尝试 | 输入Token | 输出Token | 端到端时延 | 已知费用 |
|---|---:|---:|---:|---:|---:|---:|
| `6be6506a…` | budget | 5 / 5 | 21032 | 428 | 13.739107秒 | ¥0.090976 |
| `196e4e63…` | budget | 5 / 5 | 20999 | 430 | 11.223615秒 | ¥0.090876 |
| `f863e2df…` | budget | 5 / 5 | 21098 | 469 | 11.021379秒 | ¥0.091896 |

聚合事实：

- 计划3次、完成3次，成功 **0/3**；三次主分类均为`budget`；
- 共15个模型步骤和15次Provider尝试，所有尝试索引均为1，没有SDK自动重试；
- 输入Token 63129、输出Token 1327，Usage和价格绑定全部完整；
- 已知估算费用 **¥0.273748**，远低于10元停止线，未触发费用停止；
- 时延最小11.021379秒、P50 11.223615秒、P95/最大13.739107秒；
- Provider失败、Eval基础设施失败、一般Runtime失败和未知成本均为0；
- 实际响应模型三次均精确匹配`qwen3-coder-plus-2025-09-23`。

完整脱敏计划和可重算报告分别见[`campaign-plan.json`](campaign-plan.json)与[`campaign-report.json`](campaign-report.json)。计划文件SHA-256为`751c4027fe9a22d3897e3c57e3cf99c1fedf5bb1e87f951f27efb071a9689cea`；报告文件SHA-256为`cc9ebfe2991842b801bb258b03a78900ba87dfbe60d4df03ae109236f0de2171`，与完成状态中绑定的报告摘要一致。

## 3. 根因

三次试验呈现相同路径：先运行focused测试并读取Process Artifact，再搜索OpenAI相关文件、读取`openai_chat.py`，随后尝试读取真正包含分片组装逻辑的`_chat_stream.py`。第五个模型响应提交最后一次读取调用后，Turn累计报告Token已达到21429—21567，超过任务固定的20000上限；Runtime因此在执行该读取前以`budget_exceeded`终结。

三次工作区均无文件变更，目标行为检查仍失败，身份保护回归检查通过，也没有最终结构化回答。报告中的`correctness`、`final_answer`、`forbidden_edit`和`runtime`细分类是预算终结后的评分结果；Campaign按Agent失败优先级将主分类稳定归为`budget`。

根因是**Eval任务预算与真实模型的累计输入开销不适配**，不是Provider传输、Function Calling、Usage映射、费用核算或Campaign恢复失败。离线确定性Provider每步只报告少量夹具Token，未暴露真实工具Schema、历史消息和工具结果反复进入输入所产生的累计成本。

## 4. 结论与后续门禁

本次证明了真实北京端点、精确模型、串行工具调用、完整Usage、无自动重试、三run隔离、Campaign聚合和费用核算链路可运行；不证明该模型能够或不能完成目标修复，也不能形成有效的编码成功率比较。

下一步必须先完成预算适用性设计：保持Runtime累计Token语义不变，为历史任务建立基于真实上下文开销的版本化预算，增加接近预算时的可观测证据，并在任务版本/指纹变更后重新执行独立Campaign。任何追加付费试验都需要新的次数和费用授权，不能复用本次三run授权。
