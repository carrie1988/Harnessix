---
doc_type: validation-evidence
status: current
version: 1
code_revision: eca05790fb1b99013e775dc3acd2ad03ccaf2a23
owners: [core]
modules: [models, agent, sdk, product_config, documentation]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
related_tests:
  - tests/models/test_chat_text_tool_boundary.py
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/product_config/test_git_review_fresh_owner.py
supersedes: []
---

# 商用发布跟进：业务基线归因、原完整校验成本与辅助分析

## 1. 摘要与范围

本验证没有生产源码、权限、预算或验收阈值变更。三项观察分别封存，不能合并为商用通过：

| 工作 | 结果 | 未成立的结论 |
|---|---|---|
| 原业务19项非网络失败 | 逐项JUnit与423件冻结输入源码归因，反事实执行0 | 修正后通过、生产无缺陷、无新增回归 |
| 原full-check Git单次cProfile | 1 FAIL，`git_process_timeout`，保留原期限及全部检查 | P1已关闭、无插桩SLA、批准Writer可用 |
| 人工供给5完整文件的真实分析 | 协议completed、认证重开一致；方案语义REJECTED | 自主读取成功、有效整改、Beta接受 |

主仓研究源码绑定`eca0579`。真实模型仍使用冻结N `3fb2ef57f3a647b20058f987f090e6829c4d8a58`
及其原非editable安装，不能把主仓后继Revision或其他安装证据追认给该运行。
原[全量业务FAIL](../beta-001-complete-baseline-2026-10-08-v1/README.md)、四次分析失败、
[合成配对](../native-context-pair-2026-10-08-v1/README.md)及[分层研究](../git-p1-layered-research-2026-10-08-v1/README.md)保持。

## 2. 业务前测19项逐组归因

原76类350项仍为302 PASS、9 failure、39 error、0 skipped。29项本地HTTP Mock绑定受到禁网拒绝，
本次不重跑、不豁免；另19项由原JUnit、具体源码调用点及冻结资源清单审阅：

| 组别 | 数量 | 可定位直接原因 | 后继与证据限制 |
|---|---:|---|---|
| 知识上传 | 9 | Mock stub旧读取接口，实际上传调用新的锁定读取接口，返回null后解引用 | 测试夹具契约漂移；未证明真实上传必然NPE，未改stub或重跑 |
| Training资源 | 6 | 冻结输入无`labs/`：4项模板选择拒绝、1项模板复制拒绝、1项父目录NoSuchFile | 资源闭包不足，不是EPERM；须提供有来源资源，不能用空文件制造通过 |
| 权限断言 | 3 | 新增测试用户只设置旧Role枚举，未建立动态角色及组织范围夹具 | 具体运行时assignment状态仍pending；正式新增用户流程另审，不添加GLOBAL授权凑绿 |
| Workspace名称 | 1 | 初始化已存在映射，测试只激活、不覆盖其名称 | 初始化与测试分支耦合；不据名称断言推断Cookie越权 |

423件源SHA、核查窗口身份及旧证据摘要不变；反事实测试0，未证明隐私凭据替换直接导致这些失败。
原失败集合保留，不能因已定位就把48项失败/错误减少到0。完整资源闭包、适用前测及修复后差集仍须验收。

## 3. Git P1原实现单次剖析

原[完整准备回读](../../../src/harnessix/product_config/git_prepared_link_ledger.py)及
[宿主观察](../../../src/harnessix/product_config/git_delivery_review_host.py)未启用layered桥，
Owner、physical、MAC、授权、每个callback和60秒消费者/120秒Turn期限均保持。
唯一profile窗口是实际SDK fixture的`read_all`；fixture签名seed不是生产prepared Writer。

读取主体到终端完整重验时，60秒消费者期限耗尽。profile壁钟60.000605秒、父CPU59.841757秒，
原Turn年龄约24.8→84.8秒，未到120秒审批期限。约149,005,250函数调用；
不重叠self-time中SQLite约21.996秒、物理身份及路径约20.247秒、JSON/model JSON约0.045秒。
高频本地SQL/身份检查是本场景主成本，不能把recursive函数嵌套累计耗时误称JSON解析成本。

freshOwner122,898次、Owner读取245,796次、外callback52,361次、stat2,704,844次。
失败后GitDB `total_changes=0`、行/MAC与原Session/Audit/Plans/Core/CAS/Git状态摘要相同。
事件循环最大心跳间隔27.304秒；带profile结果不是无插桩生产SLA。FD两点51→49不证明全路径无泄漏。

四个固定期望Path构造及同次closure纯URI编码是未实施的小候选，相关累计成本约2.38秒和1.21秒，
互有调用嵌套，不能相加承诺收益。只允许复用纯字符串值；不得缓存Owner、连接、物理身份、resolve结果，
不得减少回调、stat、SQL或改变首异常/期限。原生绝对不可变Path及子类回退仍须差分测试。
详细边界见[成本研究](../../research/git-prepared-cost-attribution.md)；P1及Writer/B4/B7保持开放。

## 4. 人工辅助分析：流程、接口与信任边界

5件完整核心文件由操作员从已批准的12文件输入供给；文件内容并非Agent自主读取。
原五只读工具、产品Context和原Chat mapper保持，实际工具调用0，未关闭能力或解析正文为工具。
用户Prompt正文29761字节，最多1模型请求、尝试1、无重试、步骤1、输出3072 Token，Turn120秒。
沿原最高档预留20.573440元，不以预期低费用替换预留。

```mermaid
flowchart LR
    Source[经审阅完整源码] --> Input[用户输入低信任资料]
    Input --> Protocol[原Agent Protocol与SDK]
    Protocol --> Runtime[原Context与Runtime]
    Runtime --> Guard[原费用Guard单次请求]
    Guard --> Provider[固定北京Coder]
    Provider --> Session[类型化事件及认证Session]
    Session --> Replay[原Thread重开及Replay]
    Replay --> Review[独立语义审阅]
    Review --> Reject[方案拒绝无Patch]
```

| 接口/类型 | 作用及源码 | 禁止推论 |
|---|---|---|
| `AgentClient.start_turn` | [SDK](../../../src/harnessix/sdk/agent_client.py)提交提供源码的真实请求 | 人工供给文件不转记自主read_file |
| `ThreadView` | [Protocol](../../../src/harnessix/protocol/contracts.py)公开生命周期及Usage视图 | 不是内部items容器，不据completed认定业务成功 |
| `SQLiteSessionStore.get_thread` | [Session](../../../src/harnessix/session/sqlite.py)验证后取得内部Thread/items | `user_message`不得计助手结论 |
| `authenticated_thread_history` | 同一Store认证只读复核，不initialize旧历史 | 不创建新模型请求或把未知Usage写零 |
| `GuardedVerificationProvider` | [验证Guard](../../../scripts/provider_verification_guard.py)按原价格窗口预留/结算 | 缺Usage、新未决不继续；估算不是实际账单 |

核对源码后，模型建议未获接受：FormData只是字段传输格式，不能隐藏密码；将现有JSON的
`@RequestBody`改为multipart却声称契约不变没有依据；登录后JWT不能据此证明凭据提交防重放。
[MDN FormData资料](https://developer.mozilla.org/en-US/docs/Web/API/XMLHttpRequest_API/Using_FormData_Objects)
说明其编码格式；[OWASP认证资料](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
要求凭据传输保护。没有执行该建议或生成获批Patch。

5件核心源码未发现直接输出口令的Console日志，但已存在JSON password载荷；不能推广到整个应用和部署日志。
Console、Network、HTTPS验收观察面仍须明确，文件写入白名单为空，生产TLS证据仍缺失。

## 5. 失败保全、费用与认证回读

准备阶段cwd导入错误、公开ThreadView字段错用、对不存在文件的无效负探针、隔离HOME导致钥匙串查找失败均保留。
前三类无模型请求；凭据前测失败亦在预算打开前退出。后继仅将验证进程HOME恢复实际用户目录，
原SBPL写入范围和Keychain权限不扩大；真正模型请求只有1次，没有追加重试。

认证Session只读复核输入10122/输出621，共10743 Token；原Attempt complete，
按4/16元每百万计算0.050424元，与Guard持久估算相符。认证回读模型请求0、不打开预算、不保存正文或凭据，
数据库/Key/原清单/收据/脚本及新旧账本摘要不变；共享内存侧车完整性不在声明内。
原辅助报告从账本取得的usage为null，后继认证回读单独封存，不改写原件或把null认定为零。

新60元周期26请求全部completed、估算0.706096元、预留0、剩余估算59.293904元；实际账单未知。
旧两笔未决记录和旧账本原件不变、不计入新周期，不追认零费用。

## 6. 部署观察与退出条件

Docker引擎socket仍缺失。官方start仅报告进程已运行；restart90秒终结失败，旧Backend无法退出。
在确认不存在VM进程后，对三个已核实的残留Backend发SIGTERM，旧进程退出；后继官方detached start
报告已提交，但没有引擎进程或socket，不认定恢复。Mac图形界面仍锁定，未绕过认证或系统授权。
没有容器删除/启动操作，不宣称原8容器恢复；默认Workspace挂载及全部Profile依然未验证。

后续顺序：冻结登录披露契约→适用业务前测及资源闭包→最小正式Patch与人工差异审批→最终业务回归和浏览器；
Git安全语义与响应性整改、默认Docker/Profile恢复及固定R3正式Suite另行推进。
真实任务完成数仍0，R3原严格0/20、必需检查1/20不变，R1～R6及三平台/独立Beta门禁保持。

## 7. 交付与评审

公开件：[结构事实](facts.json)、[Review Packet](REVIEW_PACKET.md)、清单；
私有原件以Manifest SHA绑定，源码/凭据/模型正文/数据库/Key不复制到公开件。
10件业务归因、28件原profile、29件辅助验证及5件认证回读均已逐SHA复验；不叠加为测试通过数。
本次为有限验证和文档同步，生产源码零改动；旧646项Models通过属于先前验证，不是本次新运行。
