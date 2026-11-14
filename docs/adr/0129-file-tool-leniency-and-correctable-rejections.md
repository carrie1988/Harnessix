---
doc_type: adr
status: reviewing
version: 2
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
  - tests/patches/test_planner.py
  - tests/patches/test_planner_newlines.py
  - tests/delivery/test_trusted_action_patch.py
  - tests/product_config/test_patch_newlines.py
  - tests/product_config/test_patch_precondition_rejection.py
  - tests/product_config/test_product_patch_rollback.py
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

第2.2节的锚定补SHA、第2.4节的规划期消息在第2版有增补（见第7节）；其余各节自第1版起未变。

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

## 7. 第2版修订（2026-11-12）：CRLF与混合换行文件上的edit；整批未应用的说明

合入主线（2026-11-14）：CRLF与混合换行的处理在主线上先行发布并记为[ADR 0141](0141-newline-aware-exact-edits.md)（同一实现），
以ADR 0141为准；本节关于整批未应用说明的部分不变，拒绝文本是写给模型的英文。

### 7.1 背景

真实用户Beta测试（PyPI 0.10.0b12，macOS TUI，百炼qwen3.8-flash，两轮共385个模型步骤）中，85次工具契约错误有68次来自
`apply_patch_batch`，其中`patch_context_not_found` 32次。

- **多行编辑在CRLF区永远匹配不上**（RT-06，`state-ocr`事件594–726）：目标文件是混合换行（804行CRLF、524行LF），被改的函数在CRLF区。
  `read_file`把原始字节逐行解码后交给模型，正文带着`\r\n`（JSON结果里是转义，紧凑文本结果里是原字符）；模型发回的`old_text`
  内容逐字正确，换行却是`\n`。规划器只做逐字节匹配，连续5次拒绝，期间没有人改过文件。
- **诊断把换行差异说成内容不一致**：缺失诊断用`strip()`比较首行，`\r`被去掉，于是得出“首个非空行在第724行，但其后的行不一致
  （可能被此前的Patch修改）”。模型据此反复重读、重发。
- **单行锚点命中后写出混合换行**（事件726）：`new_text`里的`\n`被原样写进CRLF区。测试通过后，模型又花了约2分钟修这些换行，
  其中一次编辑误删函数头后自行回滚（事件987–1086）。
- **模型发回的换行风格并不可靠**：同一处编辑，事件594的`new_text`是`\r\n`，事件684是`\n`；事件987、1029的一条`new_text`里
  两种换行混用；事件744把带`\r\n`的新行写进了LF区（单行锚点），事件1079再用一条只改换行的编辑把它们改回LF。
- **整批失败的反馈不足**（HX-13，`state-expense-expanded`事件613–919）：批次含一个新建文件和一处早已生效的编辑，同一批次连续失败18次。
  批次是原子的，新建文件始终没有落盘；拒绝说明既不指出是第几项（第1版已补上`files[i].edits[j]`），也不说明其余文件没有写入。

### 7.2 匹配：先逐字节，其次忽略CRLF/LF差异

1. 逐字节匹配仍是第一步，判定不变：恰好一处就用它，多处按锚点不唯一拒绝。逐字节恰好命中一处时不再做忽略换行的匹配
   （见7.9）；区间除第4条的情形外与此前相同。
2. 一处也没有时，把文件与`old_text`里的`\r\n`都折成`\n`再匹配一次，命中位置映射回原始字节区间；映射出的区间总是覆盖完整的
   `\r\n`，不拆开换行。忽略换行后恰好一处才接受；多处同样按不唯一拒绝并列出各处行号，不按“哪一处的换行更像”从中挑选。
3. 只折叠`\r\n`：孤立的`\r`不算换行，照旧逐字节比较。
4. 以`\n`开头的`old_text`逐字节命中`\r\n`的后半时，区间向前多取一个字节把`\r`纳入。否则按7.3改写换行会写出`\r\r\n`，
   删掉这个换行则留下孤立的`\r`。

重叠检测、无变化判定与大小上限都在原始字节区间上进行，与此前相同。

### 7.3 写入：换行风格由文件决定

`new_text`里的换行（`\r\n`或`\n`）一律按被替换处的风格写入，CRLF处保持CRLF，LF处保持LF：

| 被替换的区域 | 采用的风格 |
|---|---|
| 含有换行 | 区域内占多数的一种；两种一样多时取区域内第一个换行 |
| 不含换行 | 所在行的行尾；所在行是没有行尾的末行时取上一行的行尾 |
| 整个文件没有换行 | 不改写，按`new_text`原样写入 |

- 逐字节命中的编辑同样适用：单行锚点换成多行文本，是Beta里写出混合换行的那条路径。
- 例外只有一个：折叠换行后`old_text`与`new_text`相同的编辑，唯一可能的意图是转换换行，按原样写入（事件1079）。
  这样的编辑若只在忽略换行时命中、而文件已是`new_text`的样子，结果是如实的`patch_no_change`。
- 不按“模型是否写了`\r`”区分有意与无意：7.1的证据表明带`\r\n`抄下`old_text`的同一条编辑，`new_text`照样混用两种换行。

### 7.4 诊断

- “该编辑看起来已应用”的判定同样忽略换行：上一次写入已按文件风格改成`\r\n`，模型重发时带的是`\n`。
- 首个非空行仍在文件中的分支不再断言“其后的行不一致”（该比较忽略首尾空白，单行锚点只差缩进时也会走到这里），改为陈述
  宿主核对到的事实：`its first non-blank line (whitespace aside) is at line N, but old_text differs from the file there in more
  than line endings`。走到诊断时已确认忽略换行也找不到，所以换行差异不会再被说成内容不一致。
- 行号统一按`\n`计数，与`read_file`的分页一致（原先这一分支用`splitlines()`，孤立的`\r`会让行号偏移）。

### 7.5 前置条件补全

第2.2节“精确锚定的edit补SHA”用同一顺序判定唯一（`agent/call_repair.py`的`_anchors_once`）：先逐字匹配，一处也没有时忽略
CRLF/LF差异再匹配。否则CRLF文件上的多行锚点得不到这项补全，模型沿用旧摘要时要多一次读取与重发。只补摘要，
记录与审批的仍是模型发来的`old_text`与`new_text`；换行风格在规划时按当时的文件决定。

### 7.6 整批未应用的说明

- 已定位的拒绝（`files[i]`或`files[i].edits[j]`）之后追加一句固定英文：只有一个文件项时是`Nothing was written.`；多个文件项时是
  `Nothing was written: the batch is atomic, so the other N file entries were not applied either.`。覆盖锚点缺失、不唯一、重叠、
  超限、无变化、SHA不符、目标已存在或不存在、整文件替换过短、非UTF-8来源。
- 只陈述事实，不追加重发指令：各条拒绝已有自己的改法，有的要求删掉出错的那一项。
- 规划期的边界拒绝（`workspace_path_denied`、`delivery_path_denied`等固定码表）没有下标可用，同样追加这句。
- 未定位的拒绝（准备超时、权限位无法保留、路径只差大小写）正文里已写明本次未执行，不变；后两者是中文，直接显示在界面上。
- `workspace_parent_missing`来自`workspace/snapshot.py`的规划期观察，不在本ADR的消息表里；其处理方式由ADR 0137决定，本版未改。
- 界面文案不变：这些错误码在`product_ui/tool_summary.py`的`_USER_REASONS`里都有用户文案，英文说明不进入对话记录。

### 7.7 已保存的计划

`delivery/trusted_action.py`的`_validate_mutation`与受管Patch的`validate_prepared`会用“前镜像加编辑”重算后像，核对已保存的计划；
执行、对账和回滚都先经过这一步。7.2、7.3改变了部分编辑的重算结果（如事件726那样的事务），所以复核时传入计划记录的后像摘要
（`apply_exact_edits(recorded_sha256=...)`）：按第2版之前的语义（只逐字节匹配、`new_text`原样写入）算出该摘要的计划照旧通过，
否则按当前语义重算并照常比较。两种语义都算不出的后像仍被拒绝。没有这一步，升级前发布的这类修改在升级后无法回滚
（回滚的Turn会中断）。受管Patch的结构化Diff用同一组区间。

### 7.8 不变的部分

- 批次仍是原子的；`expected_sha256`、事务前后像与结果里的`content_sha256`都按文件的真实字节计算。
- 审批绑定的仍是模型发来的调用（`edits_sha256`），审批预览与Diff来自按上述规则算出的事务前后像。
- 回滚、恢复、`old_text`与`new_text`相同的拒绝、编辑重叠检测、各项上限不变。
- create与replace写入的字节不变；公共Schema、事件、`spec/`与产品配置不变。

### 7.9 未做与已知边界

- replace用`\n`正文整文件替换CRLF文件时，整个文件变为LF。它不产生混合换行，且`content`的摘要绑定在审批Route上，
  改写写入的字节需要改审批绑定，本版未动。
- 混合换行文件里同一段文字以两种换行各出现一次、`old_text`逐字节只命中其中一处时，按逐字节命中的那处应用，与此前相同；
  模型若本想改另一处，要从结果里的Diff发现。改为“只要忽略换行后不唯一就拒绝”会改变逐字节路径的现有结果，留作后续决定。
- 孤立的`\r`（旧Mac换行）与`\r\r\n`不做处理。
- 受管批次（`ManagedPatchBatchBridge`）的拒绝仍只有`edits[j]`：它是另一种批次语义（顺序执行、不回滚），默认产品配置不开放。
- `tool_invalid_arguments`的说明未追加“未写入”：参数不合法时调用尚未形成，原文已要求改正后重发。

### 7.10 后果

- CRLF处写入的字节比模型发来的`new_text`多（每个换行多一个`\r`）；Diff、结果摘要与后续`expected_sha256`都以真实字节为准。
- 模型在LF处有意写入的`\r\n`会被写成`\n`，除非该编辑只改换行。
- 逐字节路径上，以`\n`开头并命中`\r\n`后半的锚点，其区间起点比此前早一个字节。

### 7.11 验证

- 规划器不变量：`tests/patches/test_planner_newlines.py`（逐字节路径的区间与字节不变；忽略换行的命中映射回原始区间；
  写入风格的各条规则；只改换行的编辑；忽略换行后的唯一性；重叠；诊断；真实字节摘要；升级前保存的计划）。
- Delivery与产品路径：`tests/delivery/test_trusted_action_patch.py`（CRLF文件上的事务前后像与执行；升级前保存的事务仍可加载与执行）、
  `tests/product_config/test_product_patch_rollback.py`（升级前发布的修改仍可回滚）、
  `tests/product_config/test_patch_newlines.py`（由Beta现场缩减的回归：混合换行文件、多行LF锚点、后续单行编辑、原样抄回`\r\n`的编辑；
  多行锚点补SHA）、`tests/agent/test_call_repair.py`。
- 整批说明：`tests/product_config/test_patch_call_repair.py`（HX-13缩减的回归：指出出错项、新建文件未落盘、去掉该项后重发成功；
  各类拒绝的完整文本）、`tests/product_config/test_patch_precondition_rejection.py`。
- 现场重放（一次性核对，脚本不在仓库内）：取Beta项目中该文件的原始版本（SHA-256 `412af9c2…`，804行CRLF、524行LF）与
  事件594、684、698、712、726里模型发出的原始编辑。改动前4条多行编辑全部`patch_context_not_found`，单行编辑多出1行LF；
  改动后5条全部应用，CRLF区新增的行都是CRLF，LF区的行数只因落在LF区的那条编辑而变化。
- 未在Windows上运行：改动只处理字节串，没有平台分支。
