---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [tools, agent, delivery, patches, trusted-actions, product-ui]
related_adrs:
  - docs/adr/0050-model-correctable-tool-validation.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0093-kernel-owned-public-failure-contract.md
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
related_tests:
  - tests/agent/test_call_repair.py
  - tests/agent/test_file_observations.py
  - tests/product_config/test_patch_call_repair.py
  - tests/tools/test_argument_repair.py
  - tests/tools/test_grep_include.py
  - tests/tools/test_read_error_guidance.py
  - tests/product_ui/test_tool_summary.py
supersedes: []
---

# ADR 0129 文件工具：能唯一确定的意图直接成功，其余拒绝一次就能改对

## 1. 背景

SWE-bench Multilingual（30题×2次，qwen3.8-flash-next，产品提交447fe024）的58个有效试次中：

- 167步的全部工具调用都被拒，共99分钟；其中87步是`apply_patch_batch`，被拒的补丁平均979个输出Token，
  必须整份重发，约73分钟模型时间。
- `apply_patch_batch`参数不合法57次（含无效试次）：replace缺`expected_sha256`14次、缺`path`10次、
  `old_text`与`new_text`相同6次、`mode`是文本`"null"`5次、值为null的未知字段5次、同一文件分成两项3次、
  `files`是无法解析的JSON字符串3次。离线重放：本ADR之后其中32次通过参数校验。
- 被拒后的说明是中文，且多数不指出是哪个文件、哪处编辑：重复路径只有“参数不符合契约”，
  锚点不唯一只有“请提供更完整上下文”，无变化只有“事务包含无变化文件”。
- 读工具：`read_artifact`的`offset`/`limit`是数字字符串5次；以制表符缩进的源码行作`anchor`被当成控制字符6次；
  `glob`从仓库根遍历触发条目上限10次，其中5次的`pattern`以字面目录开头。
- Grep：990次成功搜索中265次为空；其中30次一个文件也没读（26次`include`没有匹配任何文件，分布在14个试次；
  4次`path`是单个文件而`include`把它筛掉），结果却是`scan_complete: true`的空列表。
  terraform-35611 GRMvyKf第4步在确实含有目标文本的文件里得到空结果；GQWePot第84步：“grep tool seems unreliable here”。

## 2. 决策

### 2.1 Patch：提案阶段的结构修复（`agent/call_repair.py`）

在调用形成前改写，记录、审批与执行的都是规范调用。新增：

| 模型的写法 | 改写 | 依据 |
|---|---|---|
| 值为null的未知字段（文件项或编辑项） | 去掉 | null不携带意图 |
| `mode`是文本`"null"`/`"none"`/空串 | 视为未给出 | 同上 |
| create缺`mode` | 420 | 新文件的默认权限 |
| delete带默认`mode` 420 | 去掉 | 与edit已有规则一致 |
| 文件项上直接写`old_text`/`new_text` | 包成`edits` | 只有一种读法 |
| `operation: replace`、有`edits`、没有`content` | `operation: edit` | 没有完整正文就不可能是整文件替换 |
| 同一路径的多个edit项，SHA相同或只有部分给出 | 合并为一项，编辑按出现顺序拼接 | 都针对同一前镜像；合同要求一个文件一项 |

不改的写法：带值的未知字段（如`type: "exact"`，可能是宿主无法兑现的意图）、edit同时给出`content`、
同一路径的SHA不同或混有其他操作、`files`字符串本身不是合法JSON（`argument_normalization`已处理合法的）。

### 2.2 Patch：用宿主已掌握的事实补全前置条件

沿用“精确锚定edit补SHA”的同一入口`fill_patch_preconditions`，只增加事实来源，不增加第二套机制：

- **历史中的文件摘要**（`agent/file_observations.py`）：从发给模型的历史里取各文件最近一次完整内容SHA-256，
  来源只有宿主写入的成功结果——`read_file`、带摘要的`grep`命中、Patch成功结果的`changes`。
  - replace/delete缺`expected_sha256`时补这个摘要。补的是“模型最近一次被告知的内容”，不是当前文件的摘要：
    文件此后被命令改过，规划照常以SHA不符拒绝。模型给出的SHA不改写。
  - 缺`path`而`expected_sha256`恰好是历史中唯一一个文件的最近摘要时补该路径；对应多个文件时不补。
- **现有文件的权限**：replace缺`mode`时读取现有文件权限位，属主可执行为493，否则420
  （合同只表达这两种；模型显式给出的`mode`不改）。Windows读取端没有该观察，照常拒绝。

### 2.3 `old_text`与`new_text`相同的编辑：仍然拒绝

不改为“去掉该编辑并让整批成功”。证据：babel-14532 VLJ5nVw第15步被拒后，第18步用同一`old_text`改了`new_text`
——模型本想修改，只是把新文本写进了两处。静默去掉会让它以为改动已应用；而成功结果
`PublicWorkspacePatchOutputV2`没有可携带说明的字段，加字段是公共合同变更。拒绝消息改为指出
`files[i].edits[j]`并给出两种改法（已满足就删掉这一处，否则把想要的文本写进`new_text`）。

### 2.4 拒绝消息

写给模型的说明一律英文，结构为“位置：发现了什么；最小改法”：

- 位置用模型自己调用里的下标：`files[1].edits[2]`。不回显路径、参数值或文件内容，
  延续ADR 0050/0093“公开失败不回显调用值”的约束；“发现了什么”只给宿主核对到的计数与行号。
- 参数校验（`tools/argument_feedback.py`）：具体问题排在字段清单之前；重复路径、总量超限等调用级规则
  改为固定措辞的`harnessix_`错误并带下标；操作形状按实际缺少与多余的字段生成。
- 规划期（`delivery/workspace_patch_errors.py`、`patches/planner.py`）：锚点缺失给出首行所在行号或“已应用”；
  锚点不唯一给出各处行号；重叠给出两处下标与行号；整文件替换过短给出两边字节数；SHA不符与无变化
  仍在公开边界重建固定正文，只附加经校验的`files`下标。
- 读工具（`tools/runtime.py`）：`ReadErrorCode`全部有英文说明（原来5个错误码只有“工作区读取未完成”）；
  `read_artifact`区分不存在、过期与偏移越界。

### 2.5 读工具参数

- `read_artifact`的`offset`/`limit`接受纯数字字符串，页大小截到200。存档分页核验按调用记录中的参数重算，
  所以这一修复在提案阶段完成（执行时修复会让已成功的读取被判为存档损坏）。
- `start_line`、`offset`接受纯数字字符串（不截断）；`read_file`上的`context_lines`按`lines_before`处理。
- `read_file`带着grep的`query`且全部字段都属于本步grep合同时改道`grep`。
- `anchor`允许制表符。
- `glob`从工作区根出发、`pattern`以字面目录开头时，把字面目录移到`path`（默认忽略的目录、不合法的pattern不移）。
  结果相同，但不再遍历整个仓库；字面目录不存在时由空结果变为`tool_not_found`。

### 2.6 Grep的include

- `path`是单个文件时不应用`include`。
- 起点下有文件却没有一个匹配`include`时返回失败`tool_no_files_searched`，不再返回空结果。
  选择失败而不是在成功结果里加说明：`GrepOutput`没有可用字段（`scan_complete`由合同绑定为“无截断且无缺口”），
  加字段是公共合同变更；失败在JSON与紧凑文本两种结果格式下都醒目。空目录、`include`匹配到文件但内容无命中仍是空结果。
- 两条语义以`grep_include`规则进入各Grep合同摘要（工具版本随之变化）；`glob`、`read_file`、`list_files`的
  Descriptor逐字节不变。POSIX与原生读取端共用`patterns.select_included`。

### 2.7 界面文案

对话记录原来截取失败说明的首句展示给用户。说明改为英文后，`product_ui/tool_summary.py`按错误码保留用户此前看到的
原因（`_USER_REASONS`），只在收到英文说明时使用；仍是中文的说明照原样截取。四类原说明带动态内容或同码多义，
用户文案取其固定前半句：`patch_context_not_found`、`workspace_patch_target_missing`、
`workspace_patch_edit_source_invalid`、`tool_limit_exceeded`（输出超限与扫描超限同码）。

## 3. 不变的部分

- **事务语义：** 一批文件要么全部进入同一事务，要么整批拒绝；一处编辑被拒不会应用其余编辑。
- **SHA前置条件：** 补上的SHA与模型给出的SHA走同一条核对路径（规划、审批预览、执行前各一次）；
  没有任何路径用“当前文件的摘要”替replace/delete放行。
- **审批绑定：** 修复发生在调用形成之前，Route、资源属性与审批指纹绑定的是规范参数；审批流程未改。
- **回滚与恢复：** 未触及Delivery账本、Executor、对账与`rollback_workspace_patch`。
- **公共Schema：** `WorkspacePatchInput`、各读工具输入输出Schema与`spec/`不变；校验器的判定条件不变
  （操作形状由等价的字段表表达），只改消息与错误类型码。
- **公开失败边界：** 未执行拒绝的来源标记、固定码表与“关闭Route后才报告failed”的顺序不变。

## 4. 未做

- 被拒后只重发出错的一处编辑（保留其余已校验编辑）：需要跨调用的事务状态，改变原子语义。
- `glob`在条目过万的仓库根上仍会触发上限：放宽需要改`GlobOutput.stats`的Schema上界。
- create目标已存在时改为replace、delete加create合并为replace：意图不唯一或样本只有1次。
- 权限位无法保留、路径只差大小写两类规划期拒绝仍是中文（说明来自其他交付路径共用的规划器，本次证据中0次）。
- 工具描述与Schema字段说明仍是中文（属公共Schema）。

## 5. 后果

- 模型调用的`grep`在`include`写错时由“空结果”变为失败；依赖空结果的调用方需要改看错误码。
- Grep工具版本摘要变化：升级前已记录、尚未执行的Grep调用按“合同已变化”拒绝，与历次合同变更相同。
- 失败说明含`files[i]`下标：下标指规范调用（同一文件的多项合并后）中的位置，与历史中的调用记录一致。

## 6. 验证

- 单元：`tests/agent/test_call_repair.py`（结构修复、不改的写法、三类补全、两种改道）、
  `tests/agent/test_file_observations.py`、`tests/tools/test_argument_repair.py`、`tests/tools/test_grep_include.py`
  （POSIX与原生读取端）、`tests/tools/test_schema_argument_feedback.py`、`tests/product_ui/test_tool_summary.py`。
- 产品路径：`tests/product_config/test_patch_call_repair.py`（读后替换补SHA与权限并经审批成功；读后文件被改则拒绝；
  各拒绝的下标与事实；整批不应用）、`tests/artifacts/test_runtime.py`（数字字符串分页通过存档核验）。
- 评测中观察：工具结果失败数按错误码统计；`apply_patch_batch`被拒步数与其后一步是否仍是同一补丁。
