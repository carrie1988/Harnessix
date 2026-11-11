---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product-config, evals]
related_adrs:
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
  - docs/adr/0121-external-boundary-native-process-profile.md
related_tests:
  - tests/product_config/test_working_habits.py
  - tests/product_config/test_generic_execution_context.py
  - tests/integrations/test_swebench_task_template.py
  - tests/integrations/test_harbor_pipeline.py
supersedes: []
---

# ADR 0134 默认工作习惯指令与只含评测事实的任务模板

- 状态：评审中
- 日期：2026-11-11
- 修订：[ADR 0121](0121-external-boundary-native-process-profile.md)的外部边界指令由v2升为v3；
  2026-11-09“行为规则放在评测任务模板里”的做法不再沿用。

## 1. 背景

SWE-bench Multilingual（30题，每题2次，qwen3.8-flash-next，产品提交`447fe024`）上，Harnessix解出33/58，
mini-swe-agent解出36/53。评测使用任务模板`benchmarks/harbor/harnessix/swebench-task-v3.j2`，
其中约40行是行为规则，只在评测里生效。逐条核对全部60个Harnessix试次后，结论分两类。

**被用错地方的规则**（试次名后为复盘记录的步号，与渲染轨迹的标签可能相差1）：

| v3规则 | 被怎样使用 | 试次 |
|---|---|---|
| “add no unrelated validation or options”；“A behaviour change that the issue does not ask for never justifies (a)” | 用来缩小正确的修复：看到同类实现的完整写法，只照搬一半以“保留原行为” | uutils__coreutils-6690 `AyomUtY` 13、15、24步，`3DJd6b8` 13、26步；rubocop__rubocop-13705 `w64GyQh` 38～40步 |
| “(a) … quote the sentence of the PR description” | 引用题目里的示例输出，把既有测试的新失败说成预期 | gohugoio__hugo-12204 `MNYNCbt` 79、81步 |
| “closely related variants (other argument types or orders, other call paths)” | 为没人提出的情况继续改动已经正确的修复 | google__gson-1014 `TpMBBdg` 14、15步；fmtlib__fmt-2457 `tk2eakD` 49步之后；fmtlib__fmt-3729 `wgLG5JX`（75步才第一次运行已有测试） |
| “Reproduce … outside the test suite, for example a file under /tmp … do not add repository tests” | 只能经项目自身测试环境到达的代码，在仓库外复现不了，或复现走不到出问题的那一层 | prometheus__prometheus-11859 `MULMDxt` 82～100步；hashicorp__terraform-35611 `GQWePot`；preactjs__preact-3567 `9xYoaE3` 17～47步；projectlombok__lombok-3350 `2csm7CM` 9～24步 |
| “delete the scratch files and build outputs you created … so that the final diff holds only your source change” | 还原了开始前就存在、不是自己创建的改动 | projectlombok__lombok-3009 `jGA3Cpp` 38步；另有11次不必要的还原或删除 |
| “`git stash && … ; git stash pop`” | 命令超时被终止后没有执行`pop`；`pop`后的状态输出引出上一行的还原 | prometheus__prometheus-15142 `CVeydDJ` 79～81步；lombok-3009 `jGA3Cpp` 30步 |

**起了作用、必须保留的做法：**

- 工具调用前先用一句话写出当前判断或下一步。对照分析的三个任务里，mini-swe-agent失败的试次忽略反证的那一步都没有说明文字。
- 改动前后运行同一个复现并对照（preactjs__preact-3454 `GXjTY3t` 3、5步，`mHdcRwp` 3～12步；
  babel__babel-14532 `VLJ5nVw` 27～34步据此去掉了自己多加的一处改动）。
- “改动前通过、改动后失败的测试是回归”：帮上忙2次（babel__babel-15649 `AyJ2RqG`、faker-ruby__faker-2705 `MerLzAQ`），
  没有起过反作用；改动前的基线让约14个试次没有去追查原本就失败的测试。

另一方面，产品的三种命令执行指令只描述工具与执行环境，没有上述习惯：评测里有效的做法用户拿不到，
评测测到的也不是用户拿到的产品。

## 2. 约束

1. 不为评测调参：每句话都必须是真实用户想要的工程习惯，不出现基准、题目、隐藏测试或评测的说法，不出现仓库名或语言特例。
2. 规则写清适用场合并给出可照做的做法；能由机制保证的约束不用限制做法的规则来表达。
3. 耗时与解决率同等重要：指令越短越好，每句都要有两个以上任务或语言的依据。

## 3. 决策

### 3.1 工作习惯成为产品默认指令

新增运行时指令片段`harnessix.working-habits/v1`
（[`agent_context.py`](../../src/harnessix/product_config/agent_context.py)的`_WORKING_HABITS`），全文：

```text
In every response that calls tools, first state your current hypothesis or next
step in one short line.
Reproduce a reported problem first, in the cheapest way that runs the affected code: a temporary
test in the project's own test runner or a small script, inside or outside the workspace. If that
is costly, edit first and verify after. Rerun the same reproduction after your change.
Run the existing tests nearest to the code you change, before the change and again after it. A
test that passed before and fails after your change is a regression: correct your change, unless
the user explicitly asked for that behaviour change.
Prefer the change that matches how the codebase already handles similar cases (a sibling option,
function or command, an existing convention), and match it completely. Fix the root cause, not
only the reported example, and add nothing unrelated.
When the reported case works and those tests pass, the fix is done: do not extend or rewrite it
for cases nobody asked for; mention them in your final message instead.
Before finishing, remove the temporary files you created. Leave files you did not create or
change as they are, and never revert changes that were there before you started.
```

逐句的边界与依据：

| 句子 | 适用场合与边界 | 依据 |
|---|---|---|
| 先写一句判断或下一步 | 只对带工具调用的回复；最终答复不要求写“下一步”，否则完成守卫会把它当成没做完 | 沿用v3原句，仅加适用场合 |
| 先复现，改后重跑同一个复现 | 只对“报告的问题”；方式按代价和是否走到真实代码选择，位置不限；代价高时先改后验 | 根因5的4个任务；lombok-3350、prometheus-15142在复现上耗尽步数 |
| 改动前后运行最近的已有测试；回归判断 | 基线在改动前取得，不需要`git stash`；“回归”只用于判断改动后新失败的测试，不用于设计修复 | fmt-3729、prometheus-11859、hugo `4kFtgwB`改完很久才运行或从未运行已有测试 |
| 与代码库里同类情况的做法一致，并且完整照做；修根因；不加无关内容 | 以同类实现为锚点回答“改多少”，取代只说“少改” | coreutils-6690、rubocop-13705、hugo-12204 |
| 报告的情况与这些测试通过即完成 | 不为没人提出的情况扩展或重写；发现的其他问题写进最终答复 | gson-1014、fmt-2457、rubocop-13705、prometheus-11859 |
| 只清理自己创建的临时文件；不动、不还原开始前就有的内容 | 清理对象限于“自己创建的临时文件” | lombok-3009及另外11次还原或删除 |

装配规则：

- 三种命令执行方式（容器的通用指令、本机沙箱指令、外部边界指令）共用这一份。它与命令在哪里执行无关，
  因此单独成片段、单独升版本，不复制进三份执行指令。
- Context按来源名排序，本片段排在执行指令之后：模型先读到工具与环境，再读到做法。
- 没有命令工具的固定阶段编码工作流（`harnessix.coding-instructions/v22`～`v26`）不附带：
  那里复现与测试由宿主按阶段安排，模型无法自行选择。

### 3.2 与已有指令的协调

- 外部边界指令升为`harnessix.external-boundary-execution-instructions/v3`：删除
  “Files that commands create in the workspace remain there, so keep scratch files in /tmp.”。
  “改动会保留”已由前一句说明；“临时文件只放/tmp”会禁止在仓库内用项目自己的测试方式复现。
- 通用执行指令v6的“You may use /tmp for scratch work in one command”只是许可，不作限制，保持不变；
  本机执行指令v4没有相关句子，保持不变。本机指令的“Do not commit, branch or stash unless the user asks”
  与“改动前取得基线”一致：不再需要`git stash`做前后对照。
- “示例只是示意”不单独成句：它只来自一个任务，由“与代码库已有做法一致”覆盖。

### 3.3 评测任务模板v4

新增`benchmarks/harbor/harnessix/swebench-task-v4.j2`，题目的嵌入方式与v3逐字相同，说明部分只剩任务本身和三条评测事实：

1. 答案是工作区从开始到结束的差异，包含新文件；
2. 只有非测试源码的改动计分；
3. 评测在结束时的工作区里运行它自己的测试，使用工作区已有的构建、依赖与工具配置，所以测试与这些配置结束时必须与开始时一致。

第3条按实际判分行为表述，不写成“改了也会被丢弃”：Harbor验收只重置并覆盖评测用到的测试文件，
其余测试文件与全部配置保持模型留下的样子（lombok-3009 `jGA3Cpp`还原的构建文件使判分失败；
hugo-12204 `4kFtgwB`删除的测试文件在补丁里仍是删除）。模板不含任何关于流程、验证或风格的句子。
v3保持不变，供同题对照。

### 3.4 v3各条规则的去向

| v3规则 | 去向 |
|---|---|
| 任务句“resolve the issue … general and consistent with the codebase” | 任务句留在v4；“general and consistent”由产品的“修根因”“与代码库已有做法一致”承担 |
| 只改非测试源码；不改测试与构建、依赖、工具配置 | v4第2、3条（评测自身的限制） |
| 不新增仓库测试 | 删除。限制的是做法：临时测试允许，由产品的“清理自己创建的临时文件”和v4第1、3条约束结果 |
| 在测试套件之外、例如/tmp下复现 | 产品“先复现”一句，位置不限 |
| 聚焦题目；修根因而非只修示例；不加无关校验或选项 | 产品“修根因，不只修报告的例子，不加无关内容”，并加正向锚点“与同类做法一致并完整照做”；“校验或选项”的说法删除 |
| 结束前删除自己创建的临时文件与构建产物，使最终diff只剩源码改动 | 产品“清理自己创建的临时文件；不动、不还原开始前就有的内容”；“构建产物”“最终diff只剩源码改动”删除，差异口径作为事实写在v4第1条 |
| 流程1 分析：阅读相关代码与已有测试 | 删除（没有约束力的步骤）；已有的“按实际任务选择读取、修改、复现与检查”保留 |
| 流程2 复现 | 产品“先复现”，加“代价高时先改后验” |
| 流程3 修改 | 删除（没有内容） |
| 流程4 验证：重跑复现，包括相关变体；再运行相关的已有测试 | 产品“改后重跑同一个复现”“改动前后运行最近的已有测试”；“相关变体”删除，其本意由“修根因”承担 |
| 流程5 复核失败后以简短总结结束；通过复核的修复不重做 | 产品“报告的情况与这些测试通过即完成，不为没人提出的情况扩展或重写”；总结沿用已有的“报告观察到的检查与遗留问题” |
| 用`git stash`在原代码上运行以取得基线；不要复制仓库 | 由“改动前先运行一次”取代，配方删除 |
| 按测试id与失败信息比较 | 删除：60个试次的核对里没有它影响决定的记录 |
| (a) 断言了题目明确要求改变的旧行为：保留改动，不动测试，引用原句 | 产品“除非用户明确要求了这一行为变化”；引用原句的做法删除（起反作用1次、帮上忙0次，样本中没有一例正当的(a)） |
| (b) 其他都是回归：收窄或修改修复直到测试通过 | 产品“改动前通过、改动后失败的测试是回归：改正你的改动”；“收窄”一词删除 |
| 无法引用原句即为(b)；题目未要求的行为变化不能作为(a)的理由 | 删除：被用于设计修复而不是判断失败的测试 |
| 每次回复先写一句当前判断或下一步 | 产品第一句，限定为带工具调用的回复 |

### 3.5 不在本决策范围

完成守卫、工具行为、会话开始时已有改动的清单（动态Context来源）、逐请求提示与“检查由通过变为失败”的宿主提醒，
分别由各自的决策负责。本决策只给出“不动、不还原开始前就有的内容”这条规则，清单由宿主提供后模型不必再猜。

## 4. 后果

- 所有使用命令工具的用户默认得到这些习惯；评测测到的就是产品默认行为，模板不再是评测与产品的差异来源。
- 三种执行方式的系统指令增加约1.2 KiB（约300 Token，位于可缓存的固定前缀）；评测任务说明由3049字节降到694字节。
- 改动前多运行一次最近的已有测试，换来不用`git stash`的基线，以及第一次修改后立即可比的结果。
  单次测试很慢的项目会多花这一次的时间；是否净省时间须由开发集对照确认。
- 放宽“聚焦”后模型可能多改。约束改由“不加无关内容”“通过即完成”和回归规则承担，
  开发集对照必须同时看PASS_TO_PASS回归数与导出补丁里残留的临时文件。
- 固定阶段编码工作流的指令没有变化，其中“add no speculative cases”等句子仍在；该路径没有命令工具，不是用户默认得到的路径。
- 指令只能提高做对的概率，不能保证：还原环境改动、修好又改坏两类问题仍需要宿主机制兜底。

## 5. 验证

- [`tests/product_config/test_working_habits.py`](../../tests/product_config/test_working_habits.py)：
  三种执行方式得到同一片段且排在执行指令之后；固定阶段工作流不附带；长度上限、不含评测用语；
  四类真实失败各有一条按通用形态写的回归测试（仓库内复现、清理范围、以同类做法为锚点、验证止于报告的情况）。
- [`tests/product_config/test_generic_execution_context.py`](../../tests/product_config/test_generic_execution_context.py)：
  外部边界指令v3仍与通用指令在执行环境一段以外逐字相同，且不再要求临时文件只放/tmp。
- [`tests/integrations/test_swebench_task_template.py`](../../tests/integrations/test_swebench_task_template.py)：
  v4与v3嵌入题目的方式相同，只含评测事实，不含流程、验证或风格用语。
- 行为效果只能由评测确认：开发集上以v4模板对比改动前后的产品提交，两个指标都报。
