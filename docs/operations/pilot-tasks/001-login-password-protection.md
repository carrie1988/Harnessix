---
doc_type: deployment-design
status: reviewing
version: 6
code_revision: pending
owners:
  - core
modules:
  - documentation
  - product_config
  - product_ui
  - sdk
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/app_server/test_server_sdk.py
  - tests/protocol/test_requests.py
  - tests/tools/test_scoped_runtime.py
  - tests/tools/test_search_boundaries.py
  - tests/agent/test_approvals.py
  - tests/agent/test_usage_observations.py
  - tests/product_config/test_product_patch_rollback.py
supersedes: []
---

# 首个单人 Beta 任务：登录密码传输保护

## 1. 登记契约与当前状态

本方案定义 AIPracticalPlatform 真实登录需求的单人先导登记及后续执行验收，不新增 Harnessix 架构、工具、自动化或产品能力。操作依据为[单人先导手册](../pilot-beta.md)、[文档工程规范](../../governance/documentation-standard.md)和[恢复手册](../recovery.md)。

| 字段 | 登记值与约束 |
|---|---|
| 任务 ID | `BETA-001` |
| 参与者 | 项目使用者本人，单人先导；不是独立外部 Beta 开发者 |
| 真实需求 | 浏览器中可见登录明文密码；从初始副本区分 Console 日志、错误输出与 Network Request Payload，整改必须保持原有登录业务及安全约束 |
| 登记状态 | `QUEUED` |
| 执行状态 | 限定初始源码只读分析尝试失败；业务整改与测试未执行 |
| 验收状态 | `NOT_EVALUATED`；没有 Beta PASS 结论 |
| 真实 Harnessix 完成任务数 | `0`；四次只读尝试共21个模型请求，没有整改审批或任务通过成绩 |
| 当前阶段 | 419文件初始副本和凭据形态分类已准备；完整副本仍未获外发结论。另有12文件最小只读分析输入已审阅并实际运行，原Turn失败；隔离业务测试未开始 |
| 后续执行输入 | 原Git/环境/业务数据及整改参考结果不进入模型Workspace；12文件分析输入与完整业务副本分开，分析范围可运行不等于完整副本可外发、可构建或可验收 |
| 执行候选 | 已冻结只读分析候选`7fca4a526bbbd3cde7e7c66552704126757171c2`及独立Wheel；后继整改候选仍须单独冻结，不覆盖原失败 |
| 费用状态 | [独立60元验证周期](../../changes/m09-provider-budget-period-activation.md)已登记，与R3共用；旧两笔未决不计入新额度、不阻塞新周期，旧原件保持；新增未知仍停止 |
| 生产 HTTPS | 待确认；不得假定已经部署或宣称生产整改完成 |

文档处于 `reviewing`，`code_revision: pending` 表示后继业务整改候选及完整验收前置条件尚未冻结，不否认已有只读分析尝试，也不是任务完成标记。登记与运行授权分离；排队状态不能单独触发新请求。

### 1.1 已发生的限定只读分析

真实嵌入式 SDK 经 `AgentClient → InProcessAgentTransport → AgentProtocolServer → AgentApplicationService → AgentRuntime`
调用固定百炼模型；候选、Wheel和宿主摘要见[执行证据](../../validation/beta-001-readonly-analysis-2026-10-07-v1/README.md)。
广告工具限定为 `list_files`、`read_file`、`glob`、`grep`、`read_artifact`，无写入、测试进程或远程服务能力。
原 Turn 的四步、16,000 Token、180秒预算未修改；达到步骤上限后以 `budget_exceeded` 失败，未完成分析，
尚无成功的 `read_file` 结果。原认证事件只读诊断确认四次工具均为成功的逐级`list_files`导航，
没有参数错误或`read_file`调用；[导航整改详设](../../changes/m09-r3-bounded-source-navigation.md)复用已有`glob/grep`策略，
不扩大原预算，不把指令发布当作真实分析改善。
后继[v5同条件真实复验](../../validation/beta-001-readonly-analysis-2026-10-07-v2/README.md)仍为相同四步导航且零读取，
没有观察到改善；两次共8请求、累计估算0.134792元、预留0，不再同条件重复发送。

第三次[独立新计划分析](../../validation/beta-001-readonly-analysis-2026-10-07-v3/README.md)实际11请求、9次读取，
累计103,826 Token超过独立100,000上限；末段文字同时因关键链路未读及权限误述而拒绝。
三次共19请求、累计估算0.576316元、预留0；不计完整分析、整改、测试或Beta完成。

四个模型请求均关联原Attempt和同一60元周期的预留身份，全部结算为 `completed`：累计用量价格估算
`0.072852`元、预留`0`、无新增unknown。供应商实际账单金额仍为null，不是0。
12文件前后摘要及文件身份一致；原Thread重开及3条扫描窗口分页Replay至游标66一致，包含42条公开事件。
这些事实不证明默认stdio CLI/TUI、取消、完整原目录正文未变、修复、业务测试或Beta通过；完成数仍为0。


## 2. 目标、非目标与参考证据

### 2.1 可验收目标

1. 在隔离副本中复现实际登录明文密码暴露，分别记录 Console 与 Network 结果，按实际暴露来源整改。若要求请求载荷不含明文，前后端契约必须匹配，不能仅隐藏界面、修改字段名、Base64 编码或使用可重用固定摘要掩盖密码；不能把未复现的日志缺陷当作已确认根因。
2. 由初始源码调查确定已有安全机制、登录入口、认证接口、口令验证、会话及日志链；具体整改按实际源码形成正式设计，审查兼容性与失败语义；不把既有参照实现当作固定答案。
3. 验证正确及错误密码、无效/缺失保护数据、原有验证码/会话/锁定等适用流程，避免绕过既有认证措施。重放及降级风险须结合实际方案验证；未覆盖不能宣称密码传输安全已验收。
4. 真实 Harnessix 发起协议请求、读取副本、生成整改并经人工审批后落盘；由使用者独立执行经核准的隔离测试和浏览器验收。
5. 完整证明原目录未变、写入没有越界、取消与恢复事实可核对、模型用量与费用可追溯。

生产 HTTPS 与浏览器载荷整改是不同验收项。HTTPS 保护传输链路，不能使浏览器所有者的 DevTools 无法查看提交内容；Console 日志与 Request Payload 应分项复现、分项验收，不承诺消除浏览器内存或输入框中的密码。客户端保护不能替代 HTTPS、服务端口令安全存储或日志脱敏。生产 HTTPS 未确认时，生产安全及部署验收保持未确认。

### 2.2 禁止与不计数范围

不操作原项目，不使用真实账号、业务数据、生产服务或数据库；不部署、不提交或推送目标项目 Git、不升级无关依赖、不迁移认证数据、不扩大任务为认证系统重构。登录传输契约的必要配套变更属于整改范围，须有明确兼容性设计；新增无关依赖、认证存储格式或迁移则停止并另行对齐范围。

既有 `AIPracticalPlatform-login-security-20261007` 是由 Codex 直接修改的参考结果，不是 Harnessix 自动运行成绩。其代码、补丁、整改后的测试、答案摘要及会话不得作为执行 Workspace、Agent 输入或成功预置结果。使用者可在结果形成后独立比对参考证据，必须记录参考来源与人工介入。

### 2.3 已有参考测试记录

| 参考样本 | 已报告结果 | 证据边界 |
|---|---|---|
| 既有隔离副本基线 | 350 个测试，既有 12 个失败 | 不是本任务新副本的前测 |
| 参考整改候选 | 429 个测试，12 个失败；失败集合与基线一致 | 不是 Harnessix 自动执行结果 |
| 参考新增用例 | 79 个新增用例通过 | 仅参考，不替代初始源码自主整改及最终复测 |

上述数字来源于登记输入，登记阶段未重跑；原始日志、失败 nodeid 集合、命令、环境、源码摘要与报告 SHA256 尚待绑定。不能用数量相同替代失败集合相同，不能用 79 个新增通过推导全部业务通过。未来新副本须独立取得前后测试结果；若既有失败涉及登录、安全或必需业务路径，则不得豁免该项验收。

## 3. 隔离与权限契约

| 主体或边界 | 后续执行权限 | 拒绝条件 |
|---|---|---|
| 登记维护 | 仅先导手册、本文及受控本机登记件 | 任何项目运行或原目录读取；其他源码、测试、文档写入 |
| 使用者或经授权的受信操作员 | 按既有任务授权对原目录作完整只读前后快照，并从初始源码制作独立副本 | 原目录任何写入；越过已授权任务范围 |
| Harnessix Agent | 只读工具限定 `<副本目录>`；Patch 仅写入逐项批准的相对文件白名单 | 原目录、参考整改副本、Workspace 外路径、链接逃逸、敏感文件、未广告工具 |
| 审批者 | 阅读完整差异 Artifact，核对请求指纹及文件范围，批准或拒绝单次提案 | 空白范围、陈旧摘要、越界提案、仅凭 Agent 自述批准 |
| 独立测试执行者 | 经逐项批准后在副本内运行已核对命令，所有服务、数据及输出隔离 | 默认 Agent 任意 Shell；连接原服务、真实账号、外部数据库或不可控副作用 |
| Provider | 显式选择新60元周期并确认输入范围后接收最小必要源码 | 新周期预留失败或新增未决、凭据/业务数据外发、未知端点 |

执行文件白名单当前为空，具体路径、命令、输出目录和服务依赖须从初始副本核实后逐项登记并批准；为空时不得批准任何写入。不要将目标仓库全部 `src/` 或 `tests/` 作为隐式许可。

副本必须有独立文件身份，不与原目录共用硬链接或指向原目录的符号链接、挂载、数据卷、绝对导入、测试配置或缓存输出。配置、状态、证据与备份在 Workspace 外；Provider Secret 仅通过现有受信引用取得。测试采用一次性账号标记及可清理的隔离数据，证据中不得保存密码或 Key。

完整原目录快照覆盖全部相对路径，包括隐藏、忽略、未跟踪文件与 Git 元数据；记录文件类型、大小、内容 SHA256、链接目标摘要、权限、所有者、时间及 ACL/扩展属性摘要，目录项也纳入清单。敏感文件只保存摘要，不保存正文；链接不跟随。记录不可读项和并发变化，任何覆盖缺口均不能声明原目录完整未变。读取可能改变的访问时间须单独记录；内容、目录项及其他受保护元数据必须比较。先后快照须使用同一算法和稳定观察窗口，发现外部业务写入则停止判定并重新取得有效窗口，不覆盖或“修复”原目录。

原目录快照由独立操作员保存在本机受控证据区，不向 Agent 提供原路径或敏感清单。副本可从经授权的初始源码只读备份建立，但必须证明其确为未整改输入且与已冻结来源对应。凭据、Git 元数据、业务数据及生成物不得进入模型 Workspace；排除项须有摘要和原因，禁止夹带参考整改结果。

## 4. 启动前置条件

以下条件按实际证据逐项记录；预算新周期已登记，副本整改及隔离测试范围已授权，不重复索取同范围授权：

1. 冻结经维护者交付的执行候选、安装来源、40 位 Revision、Wheel 与依赖输入 SHA256；并行候选修复未冻结时不启动，不覆盖其他维护者工作。
2. 按已批准的独立副本整改要求，核实隔离环境与初始源码来源；原目录只读快照/复制不重复索要同范围授权。原目录写入权限始终为空。
3. 取得完整前快照、全新副本清单和未整改输入来源证明，检查链接、权限、敏感内容及服务副作用边界。
4. 核实副本维护资料中的实际登录入口、测试命令及环境；填写精确文件白名单、测试隔离配置、停止时限、最多新尝试次数及人工费用上限。登记方案不提供猜测命令。
5. 核实生产 HTTPS、TLS 终止点及纯 HTTP/降级行为；不允许用浏览器载荷保护抵消 HTTPS 要求。隔离本地观察结果单独归档。
6. 使用已登记的新60元预算账本及新周期UUID，与R3共用唯一Owner，记录启动时剩余费用和预留；不要求旧两笔账单先行结算。旧原件与预留保留，新周期新增未知仍立即停止，不自动换周期或重试。
7. 按既有任务授权和新预算范围登记候选、输入及证据；新增越界操作或需求含义仍须确认，工具提案逐项核对批准。Doctor/Help成功不代替模型费用、业务或权限验收。

## 5. 后续执行步骤

| 步骤 | 操作及后置条件 | 必须保存的证据 |
|---|---|---|
| E01 冻结输入 | 完成第 4 节；从未整改初始源码创建全新副本；确认原路径不进入 Agent Workspace | 候选/输入摘要、授权收据、原目录前快照、副本初始清单、排除项及工具边界 |
| E02 独立前测 | 使用者在副本完成无副作用的复现、必要测试及浏览器观察；核实名单和命令后批准任务范围 | 精确文件白名单、实际命令/cwd/环境摘要、退出码、用例 nodeid、已知失败、脱敏复现 |
| E03 真实会话 | 使用隔离安装及独立状态根，按先导手册预检；完成 `initialize` 与 `notifications/initialized`，保存广告能力；用 `thread/create` 创建绑定副本的新 Thread | Doctor 收据、协议版本、客户端身份、服务能力、Thread ID、Workspace 摘要 |
| E04 分析与中断观察 | 通过 `turn/start` 提交真实缺陷及范围，不提供参考答案；在必要只读阶段发 `turn/cancel`，等待终态后检查副本；重新连接原 Thread 并 Replay | 请求 ID/指纹、Turn ID、模型尝试/用量、工具调用与作用域、取消收据、前后文件摘要、游标 |
| E05 独立整改 | 确认取消已收敛且费用允许后，以明确的新请求创建整改 Turn；Agent 自主调查已有前后端契约、提出最小 Patch，使用者审阅完整差异后批准 | 新旧 Turn 关系、Plan/审批/Artifact ID 及摘要、批准/拒绝记录、实际文件效果、人工介入 |
| E06 最终复测 | 使用者执行批准的单元、接口、回归及浏览器隔离验收；保存服务关闭与数据清理结果，不操作生产 | 修复后源码清单、前后失败集合、浏览器脱敏证据、安全检查、测试报告及 SHA256 |
| E07 重开与保全 | 退出/重开原 Thread，核对历史、审批、Artifact 与耐久游标；如需完整状态恢复，仅在另行批准的停机演练环境按恢复手册执行 | 原 Thread/Turn 对应关系、Replay 范围、重开前后文件摘要；适用时 backup/restore ID 与验证报告 |
| E08 人工验收 | 独立操作员取得原目录后快照并比较；使用者逐项审阅验证矩阵、费用及残留限制，签署接受或拒绝 | 原目录对比、范围核对、费用状态、使用者验收身份/时间/结论及未验证项 |

E04 若错过取消窗口，标记 `NOT_OBSERVED`，不得记取消通过；需要后续授权的真实必要分析尝试补足，不能制造危险写入来观察取消。`Ctrl+Q` 或断线不是取消证据。E07 的重开不等于数据库恢复；需要恢复时不得在唯一生产状态上演练，也不能依靠状态恢复撤销 Workspace 效果。

协议方法及恢复约束以第 9 节源码和[先导恢复步骤](../pilot-beta.md#7-失败取消重开与状态恢复)为准。`turn/retry` 是新 Turn，`turn/resume` 仅适用于原 Turn 的合法恢复状态；响应丢失先查询/Replay，不盲目重复提交。相同命令重传沿用原客户端及请求身份，不用新 request_id 掩盖未决效果。所有新增尝试都保留为本任务内尝试，不另计真实任务完成数。

## 6. 失败、取消与权限处置

| 情形 | 处置 | 记录及禁止事项 |
|---|---|---|
| 未授权、路径越界、链接逃逸、权限不足 | 拒绝读取/审批并停止；确认没有副作用 | 记录调用范围和稳定错误代码；不放宽权限、不改成管理员运行 |
| 原目录被触及或敏感材料泄露 | 立即停止，保全受控证据，按先导 P0/P1 分诊 | 不补写原目录、不删除痕迹、不外发原始载荷 |
| Patch 前像不符、文件被他人修改或差异不完整 | 拒绝原提案，重新核对副本与范围 | 不覆盖他人修改；不使用旧审批身份授权新内容 |
| 等待审批被拒绝/取消 | 等待服务端终态，核对是否写入 | 拒绝也是有效观察；未完成任务，不算业务接受 |
| 网络、认证、超时、用量缺失或费用上限 | 停止新请求，查询已有事实并对账 | 原预留不释放、预算不修改；失败或取消不等于零费用 |
| `unknown`、部分效果、恢复未决 | 标记结果不确定，停止新效果；由维护者核对事实 | 不 Retry、不硬重置、不恢复覆盖原目录 |
| 测试需访问业务服务或破坏数据 | 不运行该命令，登记阻塞及所需隔离条件 | 不以单元绿色替代业务验收；不使用真实账号绕过 |
| 新失败、认证退化或 HTTPS 未确认 | 不接受相应验收项，保留原失败集合和风险 | 不隐去既有 12 失败，不宣称生产安全或已部署 |
| 取消/恢复证据缺失 | 标记 `NOT_OBSERVED`/`BLOCKED`，另行批准补证 | 不用源码合同测试、模拟 Provider 或直接 Codex 修改替代真实运行 |

## 7. 验证矩阵与通过规则

以下矩阵定义完整任务验收；现有只读分析尝试只产生候选、协议、费用及分析输入完整性观察，尚未满足任何完整任务验收项。验收不以测试总数或模型回复代替证据。

| ID | 验证内容 | 接受条件 | 证据 |
|---|---|---|---|
| V01 | 来源与防污染 | 全新副本来自冻结未整改源码；没有参考答案输入 | 来源/副本清单摘要、排除表、输入审阅记录 |
| V02 | 原目录完整性与工具边界 | 完整前后快照没有受保护变化；无越界读取/写入；副本文件身份独立 | 原目录完整对比、工具作用域、链接/权限检查 |
| V03 | 登录载荷与泄露 | Console、错误输出、URL、响应、日志、存储和证据均无明文口令；Request Payload 单列结果，若按载荷保护契约整改则不得含明文，不能仅编码或换字段名 | 脱敏载荷结构、Console 检查、测试结果、日志/存储检查摘要 |
| V04 | 认证语义与滥用 | 正确密码成功，错误密码失败；无效/缺失/篡改输入、重放及降级按已审阅契约拒绝；既有验证码、会话、锁定等适用流程不退化 | 单元及接口 nodeid、隔离数据、前后结果 |
| V05 | HTTPS 边界 | 明确部署环境及 TLS 终止点，凭据路径无纯 HTTP/降级；未取得证据则生产项不得接受 | 使用者环境确认及合法 TLS 证据，不是部署推定 |
| V06 | 真实协议及审批 | 真实 Harnessix 的握手、Thread、Turn、工具结果及完整差异人工审批可以闭环 | 关联 ID/指纹、Artifact SHA256、耐久事件及实际效果 |
| V07 | 最终测试与非破坏性 | 必需安全/业务项通过，无新增失败；既有失败精确归因并获人工审阅，不能豁免登录安全失败；所有服务及输出隔离 | 前后命令、报告、退出码、nodeid 集合与差集、清理记录 |
| V08 | 模型费用 | 新60元周期和用途绑定，新尝试含取消/重试的用量、估算及预留完整；旧未决不计入新额度；无权威账单时实际扣款标记未确认，未知不记零 | 新周期/attempt/response ID、用量完整性、估算价格、可用额度与预留；账单引用如已取得 |
| V09 | 取消与重开/恢复 | 真实取消收敛、无重复效果；重开后历史/审批一致；完整恢复若列为必需则按批准方案取得独立证据 | 取消终态、文件摘要、Replay 游标、适用恢复收据 |
| V10 | 使用者人工验收 | 使用者核对需求、允许差异、测试、原目录、费用及限制，并明确接受 | 验收身份、时间、检查项及结论，不以 Agent 自评代替 |

本地整改任务接受至少要求 V01～V04、V06～V10 全部满足，且生产 HTTPS 未确认的边界已明确记录；V05 未确认时最多形成隔离副本整改验收，不得形成生产安全结论。若使用者将生产 HTTPS 设为任务必需条件，则 V05 也是完成计数前置条件，执行前必须冻结该选择。当前 HTTPS 范围选择未确认，不能自行采用较宽松标准。

登记状态只能在前置条件及授权具备后由 `QUEUED/NOT_EXECUTED` 转为执行中；失败、阻塞、取消及结果不确定均单独保留。只有矩阵所需证据齐全、费用已按既有规则可核对且使用者明确接受后，才可将任务记为 `ACCEPTED` 并把本任务计数增加 1；Harnessix Turn 的 `completed` 不等于人工接受。真实任务只计一次，多 Turn/重试不增加任务数。

## 8. 证据字段与本机登记件

本机 `beta-task-001.json` 是结构化登记，`beta-registration.md` 是登记摘要及后续证据导航；两者位于同一受控验证目录。实际路径只保存在本机登记件，公共资料使用 `<源目录>`、`<副本目录>`、`<证据目录>`。所有运行字段当前为空或未执行；不能用样例 UUID、费用零值或参考报告填充真实结果。

| 字段组 | 必填字段与约束 |
|---|---|
| 身份/状态 | schema_version、task_id、参与者角色、registered_at、registration_status、execution_status、acceptance_status、任务完成数 |
| 来源/候选 | 初始来源证明、候选 Revision、Wheel/安装输入 SHA256、副本初始清单、参考证据隔离标记；缺失用 null |
| 原目录完整性 | 前后 manifest 引用及 SHA256、算法/覆盖范围、快照时间、不可读项、差异、并发变化、操作员与授权身份 |
| 边界/测试 | 具体相对文件白名单、批准命令、cwd、隔离输出/数据/网络范围、环境摘要；每项批准与时间 |
| 真实协议 | protocol_version、client_instance_id、广告能力、thread_id、turn_id、request_id/指纹、耐久事件游标及终态 |
| 审批/效果 | Plan/approval/call/Artifact ID、请求指纹、Artifact SHA256/完整性、批准或拒绝、前像/后像、实际差异与人工介入 |
| 模型/费用 | Provider/模型、attempt/response ID、输入/输出/总 Token、完整性、起止时间、失败/取消、新请求数量、权威费用/币种/账单引用、新旧周期隔离及预留处理依据；未知用 null |
| 取消/恢复 | 取消 request_id/终态/收敛时间、重开 Thread、Replay 范围；适用时 backup_id/restore_id/验证结果；不得预填通过 |
| 测试/验收 | 前后命令及报告摘要、失败 nodeid 集合/差集、浏览器证据、矩阵逐项结果、使用者身份/时间/结论、未验证项 |
| 证据制品 | 每件制品的相对或受控本机路径、SHA256、大小、采集时间、来源、脱敏状态、审阅者；不保存密码、Key 或未审阅原始载荷 |

## 9. 源码与测试映射

以下链接对应隔离候选中可核对的实现与合同测试，仅说明现有能力边界；登记阶段没有执行这些测试，不把测试夹具当作真实 Beta 证据。

| 设计元素 | 源码及关键符号 | 验证测试及符号 | 说明 |
|---|---|---|---|
| 默认 Workspace 装配 | [server.py](../../../src/harnessix/product_config/server.py)，`_serve_product_stdio` | [test_scoped_runtime.py](../../../tests/tools/test_scoped_runtime.py)，`test_coding_scope_mismatch_fails_before_target_io` | 执行仍须核对冻结候选及实际工具定义；不得假定可执行任意 Shell |
| 只读路径/链接边界 | [workspace.py](../../../src/harnessix/tools/workspace.py)，`WorkspaceReadPolicy.parts`；[runtime.py](../../../src/harnessix/tools/runtime.py)，`CodingToolRuntime` | [test_search_boundaries.py](../../../tests/tools/test_search_boundaries.py)，`test_ignore_is_not_permission_and_links_never_escape` | Ignore 不是权限控制，不能代替白名单或原目录隔离 |
| 握手/真实 Turn | [agent_client.py](../../../src/harnessix/sdk/agent_client.py)，`AgentClient.initialize`、`create_thread`、`start_turn` | [test_server_sdk.py](../../../tests/app_server/test_server_sdk.py)，`test_handshake_enforces_state_version_and_params`、`test_agent_sdk_drives_turn_replay_and_duplicate_command` | 实际 run 必须绑定 Thread/Turn/请求身份 |
| 请求幂等 | [requests.py](../../../src/harnessix/protocol/requests.py)，`SQLiteProtocolRequestStore.claim`、`request_fingerprint` | [test_requests.py](../../../tests/protocol/test_requests.py)，`test_same_request_id_with_different_command_is_rejected` | 响应丢失先查询，不凭新 ID 盲重试 |
| 完整 Patch 审批 | [workspace_patch_review.py](../../../src/harnessix/product_config/workspace_patch_review.py)，`WorkspacePatchReviewProvider`；[agent_bridge.py](../../../src/harnessix/patches/agent_bridge.py)，`ManagedPatchBridge` | [test_approvals.py](../../../tests/agent/test_approvals.py)，`test_approval_is_durable_before_execution_and_excluded_from_model` | 人工阅读完整 Artifact，审批不能复用于变化的前像/请求 |
| 取消/重开 | [agent_client.py](../../../src/harnessix/sdk/agent_client.py)，`AgentClient.cancel_turn`、`resume_turn`、`retry_turn`、`replay_events` | [test_approvals.py](../../../tests/agent/test_approvals.py)，`test_cancel_parked_turn_and_reject_late_reply`、`test_restart_preserves_wait_and_only_resumes_pending_call` | 取消不撤销已经发生的效果；Retry 与 Resume 不同 |
| 漂移拒绝/效果核对 | [workspace_patch_source.py](../../../src/harnessix/product_config/workspace_patch_source.py)，`load_owned_workspace_patch` | [test_product_patch_rollback.py](../../../tests/product_config/test_product_patch_rollback.py)，`test_waiting_rollback_and_approved_drift_do_not_overwrite` | 引用现有拒绝语义，不授权自动回滚，更不授权改原目录 |
| 用量/未知费用 | [usage.py](../../../src/harnessix/agent/usage.py)，`ModelAttempt`、`ModelUsageObserved`；[interactions.py](../../../src/harnessix/product_ui/interactions.py)，`UsageCostView`；[contracts.py](../../../src/harnessix/protocol/contracts.py)，`PublicBudget` | [test_usage_observations.py](../../../tests/agent/test_usage_observations.py)，`test_unknown_zero_and_missing_details_are_distinct` | Token、UI unknown 费用、Provider 实际账单分别记录 |

AIPracticalPlatform 的实际入口、认证接口和测试文件尚未从全新副本核对；登记阶段不读取原目录、不伪造源码链接。E02 必须在本机证据中填入副本内相对文件、完整符号、源码摘要和测试 nodeid，核实可打开且对应冻结初始版本后方可批准写入。登记文档中的 Harnessix 链接不冒充目标项目证据。

## 10. 发布门禁及剩余条件

单人任务最多产生一个先导任务的验收记录，不替代原 R5 的 **3～5 名独立开发者、至少 15 个真实任务、三平台实际使用及无未处置 P0/P1**。R3/R4 候选条件、R6 封板及商业发布结论不因本登记改变。

当前剩余条件包括导航整改的后继真实验证、业务整改候选冻结、完整业务副本及原目录全内容隔离证明、真实源码/测试映射、HTTPS范围确认、取消观察、完整整改审批、最终测试与使用者验收。已有只读分析协议及重开观察不关闭这些条件。完成数保持0；既有参考结果、绿色测试及本方案的链接检查均不构成Beta PASS。

### 核心链与原生工具完成门复核

第四次[独立核心链读取计划](../../validation/beta-001-readonly-analysis-2026-10-07-v4/README.md)在两次模型请求后Turn completed，
但仅核心1/5文件首页，最后文字是工具标记而非分析；宿主技术门正确拒绝，未将文字执行为工具。
四次共21请求、累计估算0.620748元、预留0、账单未确认；实际任务完成仍0。原失败保留，停止同类重复调用并先定位协议/模型行为。
