---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5e26f952ead508dcf003c73fc54e717c6a4169d7
owners: [core]
modules: [models, agent, session, context, protocol, app_server, sdk, product_ui, product_config, evals]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/models/test_tool_rejection.py
  - tests/agent/test_tool_rejection_runtime.py
  - tests/agent/test_rejection_recovery.py
  - tests/context/test_rejection_fork_v2.py
  - tests/session/test_rejection_publication_versions.py
  - tests/product_config/test_rejection_backup_versions.py
  - tests/app_server/test_rejection_sdk_chain.py
  - tests/app_server/test_handshake_protocol_v2.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_config/test_git_prepared_link_ledger.py
supersedes: []
---

# R3类型化拒绝、Protocol 2.0与安装态闭环交付报告

## 1. 结论与范围

本切片实现结构合法但未登记工具调用的不可执行拒绝及固定失败反馈，贯穿两Provider Adapter、Kernel、认证Session、
Context/Fork、公共协议和内置SDK/UI。原预算、期限、调用目录、来源认证及普通工具审批不放宽。
旧Agent Protocol 1.0客户端在握手时明确拒绝，不静默转换历史、不提供双栈或名称模糊匹配。

同一最终Wheel已正式安装并完成以下终态验证；源码、重复候选及独立集合不合计为独立用例数。备份318、SDK81和产品CLI16合并一次执行，
原JUnit为414通过/1跳过；下表三行来自其互斥classname子集，不补造独立运行：

| 验证集合 | 通过 | 失败/错误 | 跳过 | 边界 |
|---|---:|---:|---:|---|
| 完整Agent/Context/Session/Models/Protocol/AppServer/ProductUI七目录 | 2763 | 0/0 | 1 | 唯一跳过为原生Windows Context Handle |
| 备份、迁移清单、父闭包、原验证预算及产品收敛 | 318 | 0/0 | 0 | 完整适用文件，无放宽预算 |
| R4实际认证SDK消费者 | 2 | 0/0 | 0 | prepared关联COMMIT/非空只读重开与原异常身份；非approved Writer |
| Artifact/Patch SDK、公开CLI、Eval CLI及Soak契约消费者 | 81 | 0/0 | 0 | 契约回归，不是实际稳态Soak成绩 |
| 完整产品Server/CLI测试文件 | 15 | 0/0 | 1 | 跳过为原生Windows默认Git/SDK装配 |

R3真实供应商历史接受性、完整20 Trial质量复验和R4完整Git授权写链、三平台原生编码仍开放。
**原R3严格0/20、必需测试1/20、真实Beta接受零保持，正式商用为NO-GO。** 本切片新增付费模型请求为零。

## 2. 背景、根因与正式契约

原未知工具诊断发生在参数类型和严格JSON校验之前，不足以证明未知调用结构完整。
合法但不在本次精确广告目录的调用需要可恢复反馈，而不能复用普通Tool Call或透传原名称绕过Alias。

| 合同 | 当前版本 | 变化 |
|---|---|---|
| Provider Event | v4 | ToolCallRejected仅call_id、固定reason和必填argument_chars |
| Agent Event/Thread | v21 | 持久拒绝Item、固定结果、证据化tool_rejection重入 |
| Thread Fork | v2 | 只读继承闭合拒绝；v1类型和既有确定性身份保留 |
| Agent Protocol | 2.0 | 公共拒绝仅UUID、step、reason；Server、SDK、CLI/UI配套 |
| 公共Schema | 六份新v2 | 旧十四份v1及旧Event/Thread20、Provider3、Fork1共十八份原字节冻结 |
| Session Migration | 0031 | 语义版本界标，无新增业务表；旧Reader不能接管新历史 |

公共Thread等未变化结构仍为v1；结构版本、连接协议版本与JSON-RPC Envelope的2.0是不同合同。
完整需求、目标、方案取舍、接口、字段、伪代码、异常、安全和部署见[总体及详细设计](../../changes/m09-r3-unknown-tool-recovery.md)。

## 3. 架构、时序与数据流程

Adapter全组验证严格终态、响应身份、ID、index、function与JSON object，然后按精确目录分类。
Runtime暂存全部普通/拒绝提案，只有流正常关闭才以一个原CAS批次提交提案组及固定拒绝结果。
已知Usage沿原独立路径保留；异常尾不能释放普通工具或拒绝事实。只有拒绝的步骤经原Guard进入新模型步骤，
混合普通工具仍经过原准入、审批及执行，不赋予拒绝执行权。

认证Session在原MAC及完整正文摘要验证后唯一解析事件，闭合检查覆盖追加、Replay尾、Snapshot、Backup和Fork。
Context使用固定未广告标记和空参数反馈，公开投影去除Provider私有ID，UI只显示正式拒绝事实。

四个详设图均成功编译，架构图另完成视觉检查；可放大查看
[总体架构](diagrams/diagram-1.svg)、[完整时序](diagrams/diagram-2.svg)、
[失败与恢复](diagrams/diagram-3.svg)、[数据与安全边界](diagrams/diagram-4.svg)。

### 3.1 源码阅读入口

| 责任 | 入口与重点 |
|---|---|
| 结构校验、目录分类 | [_chat_stream.py](../../../src/harnessix/models/_chat_stream.py)、[_anthropic_stream.py](../../../src/harnessix/models/_anthropic_stream.py) |
| 配对历史与固定标记 | [_history.py](../../../src/harnessix/models/_history.py)、[tool_result_view.py](../../../src/harnessix/context/tool_result_view.py) |
| 原子提交与有证据重入 | [runtime.py](../../../src/harnessix/agent/runtime.py)的_sample_events/_close_model_step、[turn_reducer.py](../../../src/harnessix/agent/turn_reducer.py) |
| 固定结果与闭合规则 | [tool_rejections.py](../../../src/harnessix/agent/tool_rejections.py)、[item_reducer.py](../../../src/harnessix/agent/item_reducer.py) |
| 原来源与版本绑定 | [publication_seal.py](../../../src/harnessix/session/publication_seal.py)、[sqlite_publication.py](../../../src/harnessix/session/sqlite_publication.py) |
| Fork身份与旧合同 | [lifecycle.py](../../../src/harnessix/agent/lifecycle.py)、[models.py](../../../src/harnessix/agent/models.py)、[event_compatibility.py](../../../src/harnessix/agent/event_compatibility.py) |
| 公共出口及握手 | [projection.py](../../../src/harnessix/protocol/projection.py)、[handshake.py](../../../src/harnessix/app_server/handshake.py)、[rendering.py](../../../src/harnessix/product_ui/rendering.py) |
| 备份与迁移清单 | [state_backup_validation.py](../../../src/harnessix/product_config/state_backup_validation.py)、[0031](../../../src/harnessix/session/migrations/0031_unknown_tool_rejection.sql) |

## 4. 真实组件验证与质量证据的区别

新增SDK闭环使用实际OpenAI Adapter与HTTP固定Wire替身、认证SQLite、App Server、Agent SDK和UI，
包括拒绝、下一步骤纠正、一个正常只读工具、第三步骤回答、重复命令、Replay/Next及无模型请求的重开。
固定未知名称和参数不进入持久/公共/下一模型历史，标记不被广告为工具，拒绝前没有工具执行。
这是真实组件集成，不是真实供应商或真实编码质量成绩。

硬退出测试覆盖终态前、after_events、after_projection、after_commit和after_tool_call。
未提交时没有拒绝配对，提交后完整配对；取消与原超时代码保持，重开不自动重发、重试或执行拒绝。
旧Reader负控使用冻结旧Migration资源，不能扩大为所有已发布旧程序的实测兼容。

## 5. 候选、安装环境与验证边界

最终Wheel SHA-256：`67343ba31d181d71140ec83b82daf80ded777eb350c4e9d623645867badb87f9`。
版本仍为1.0.0rc1，Python3.12.8/macOS、Git2.53.0，依赖离线安装。
全部554个生产成员逐字节等同源码及正式site-packages，`python -I -B`验证安装导入。
Finder和字节码元数据不属于Wheel生产成员，排除项明确记录。Wheel说明元数据也与当前README准确匹配。
父pytest未用PYTHONPATH覆盖产品包；部分显式硬退出测试子进程引用同字节源码，不宣称所有子进程均Wheel隔离。

七目录安装回归JUnit为110.494秒，R4实际消费者为163.395秒；并行竞争下耗时不外推P1/SLA。
512个源码文件类型检查、适用Ruff、Schema一致性和十八份旧Schema摘要验证通过。
公开命令和原件摘要见[verification.json](verification.json)；私有原件保留宿主和失败正文，不公开客户源码或凭据。

## 6. 中间失败与不可扩大解释

原初始拒绝合同RED、Context/完整回归旧版本断言、Adapter旧失败预期及恢复测试误写字段均保留。
旧普通、旧Reader和畸形组负控没有删除；Models保留原822项并新增39项历史结果负控。
选择不存在路径返回退出4是未执行测试，不是PASS；两次安装身份脚本错误仅是诊断脚本修正，未改变产品或门槛。
产品Artifact正控原1.0握手按新合同拒绝，正控改为2.0；独立1.0/未知版本负控仍完整保留。

格式收尾发现两个文件未规范，其中一个为产品源码。格式化后重新构建新Wheel并重新运行全部安装集合，
旧`74311b…`与说明元数据同步前`25b190…`候选及各自通过记录均为中间证据，不冒充最终字节。
同步说明元数据后再次正式安装并重新执行完整七目录、产品消费者及R4消费者，最终候选只绑定最后原件。
原R4长消费者B为31/31、3085.687秒；它使用早于本切片的中间源码覆盖，不证明新Wheel、FD、原锁或Writer。

## 7. 升级、回退与遗留风险

Server、SDK、CLI/UI必须使用匹配候选升级，旧1.0返回unsupported_protocol_version，不暗中降级。
写入v21/migration31前保留完整匹配备份，原20认证字节保持；回退恢复匹配旧备份及旧程序，不删除新历史或手改版本。
详见[升级与回退手册](../../operations/upgrade-and-rollback.md)。

- R3：真实供应商必须接受未广告的固定历史标记；不能通过广告伪工具绕过失败。
- R3：需同一固定候选完整真实20 Trial及原样评分；费用未决预留不据离线测试释放。
  新60元周期只读核对：28请求/27 completed/1 unknown，已知估算0.725956元、预留20.77824元、
  剩余估算38.495804元；未登记本周期有界复验合同，默认暂停真实请求，不沿用旧周期授权。
- R4：B4/B7实际FD、锁归属、P1、approved Writer及默认Git完整授权写链尚未完成。
- 平台：本机跳过的Windows原生用例及三平台实际编码、最终候选升级/恢复仍独立开放。
- 本次未运行CI验收，不把本地结果等同CI、真实Beta或商用发布。

## 8. 交付复核

[facts.json](facts.json)记录候选身份、终态、旧合同摘要及质量门禁；[verification.json](verification.json)记录命令与边界；
[REVIEW_PACKET.md](REVIEW_PACKET.md)列出接受事实、拒绝扩大解释及下一步；[manifest.json](manifest.json)绑定公开成员。
完整源码设计见[架构](../../architecture.md)及[详设](../../changes/m09-r3-unknown-tool-recovery.md)。
原始私有证据、中间候选和既有封存保持独立，不覆盖原失败或旧质量记录。
