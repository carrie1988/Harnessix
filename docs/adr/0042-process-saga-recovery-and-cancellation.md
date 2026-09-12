---
doc_type: adr
status: current
version: 1
code_revision: f00b554fbe6a27d77a0e02ce688c8253ea24aa13
owners:
  - core
modules:
  - agent
  - processes
  - storage
related_adrs: []
related_tests:
  - tests/processes
  - tests/agent
  - tests/integration
supersedes: []
---

# ADR 0042：Process跨库恢复、等待取消与SDK闭环

- 日期：2026-09-06
- 基线：`048a231`，CI 33987803502 四项通过，开工 `fetch` 一致
- 状态：已采纳并实现（0.5.4b2c3）

## 1. 问题与目标

0.5.4b2c1已经接通模型调用、唯一Action审批、独立Worker和有界终态观察，b2c2已经把Process输出作为事务Artifact发布。但两个SQLite库或Session与PostgreSQL之间没有分布式事务，以下窗口仍必须有明确语义：

1. 模型ToolCall已经进入Session，Action尚未创建或已创建但审批Item尚未提交；
2. Action审批决定已提交，Session仍显示未决定；
3. Worker执行期间宿主退出，租约过期后Action只有UNKNOWN状态而没有可投影结果；
4. 用户在WAITING_APPROVAL或WAITING_ACTION取消Session等待；
5. 多个API进程同时决定同一Action审批；
6. OpenAI与Anthropic实际SDK经过重开、审批、外部Worker和Artifact读取时，模型wire不能携带私有Action证据。

本片目标是在不新增协调器、数据库表或分布式事务的前提下关闭这些窗口。成功标准不是“尽量继续”，而是：同一命令最多执行一次；每个持久状态可解释；证据不足时停止模型循环；恢复不创建第二份许可或自动重放非幂等进程。

## 2. 不变量与模块边界

以下不变量保持不变：

- Effect Journal中的`ApprovalRecord`仍是唯一执行许可；Session审批只做UI、Replay和调用归属投影。
- `AgentRuntime`不能领取READY、执行命令、续租或按历史PID发信号；只有独立`ActionWorker`可执行。
- `ProcessAgentBridge`只做稳定Action准备、唯一决定、决定同步和单次观察；完整argv只存在原ToolCall/ActionRequest。
- Session和Effect Journal分别保证本地事务原子性，跨库使用可重入Saga，不宣称原子提交。
- UNKNOWN与MANUAL_INTERVENTION不可自动回READY；非幂等命令没有证明时不得重放。
- Process Artifact是展示副本，不是执行事实或授权凭证。

本片没有改变Agent Event/Thread v9、Session migration11、Action Contract v1、Process v1或Artifact v1 Schema，也没有新增依赖和中间件。

## 3. Action准备缺口恢复

Runtime启动时若发现Turn处于`EXECUTING_TOOLS`，首个未结算调用为`host.process`、效果分类为`NON_IDEMPOTENT_WRITE`且没有Session审批Item，则进入专用恢复分支：

1. 未配置原`ProcessRuntime`端口时保持原事件与状态，不把调用结算为“未执行”，也不创建Action；
2. 配置原端口后，按Thread/Turn/Call、工作区、工具版本、Principal、argv摘要和超时重新构造稳定Action身份；
3. `ActionService.submit`以确定性Action ID和幂等键返回原Action或创建唯一Action；
4. Runtime只补交Session审批请求并进入WAITING_APPROVAL，不执行Action。

若Action已由其他进程决定或已经运行，`prepare`仍先创建一条未决定的Session投影；随后`resume_turn`通过`sync_decision`读取权威Action决定并进入WAITING_ACTION。这样可恢复“Action决定已经存在、Session连审批Item都没有”的窗口，同时避免把已有决定伪装成新的客户端答复。

任何调用、作用域、Principal、Action指纹、工具版本或宿主绑定漂移仍以`process_projection_mismatch`关闭，不允许按相似命令寻找替代Action。

## 4. 完整跨库退出矩阵

| 退出点 | 重启时事实 | 恢复结果 |
|---|---|---|
| Action准备前 | Session有ToolCall；无Action | 缺端口保持；有端口创建稳定Action并补审批请求 |
| Action提交后、Session审批前 | Action为PENDING；Session无审批 | 重取同一Action并补审批请求 |
| Session审批请求提交后 | PENDING + WAITING_APPROVAL | 原样等待，不猜用户决定 |
| Action决定提交前 | PENDING + WAITING_APPROVAL | 用户可按原请求决定；超时后只能取消或人工处理 |
| Action决定提交后、Session决定前 | Action已有唯一决定；Session未决定 | `resume_turn`只读同步，不再次决定 |
| Session决定提交后、Worker前/运行中 | READY/LEASED/RUNNING + WAITING_ACTION | 单次观察后继续等待，不轮询、不重放 |
| Action终态后、Session结果前 | Action有终态结果；Session仍等待 | 生成一次状态、ToolResult和可选Artifact事务批次 |
| Session结果提交后、第二模型步骤前 | Session已有ToolResult；模型续跑未提交 | 启动保守记为INTERRUPTED，不自动重发模型请求 |

真实退出测试在上述八个提交边界调用`os._exit(88)`。每个场景重开同一Session和Effect Journal后都保持一个Action、一次命令标记、至多一个Artifact和确定性Replay。最后一行刻意不自动续跑模型：模型调用本身尚无跨进程attempt接管协议，自动调用可能产生新ToolCall；已成功Action事实不会因此被改写。

## 5. WAITING取消和时间语义

取消只终止当前Agent对Action的等待，不是Action撤销协议。WAITING_APPROVAL或WAITING_ACTION取消时，Runtime：

1. 先持久化`CANCELLING`；
2. 为原Call写入`outcome=unknown`的保守ToolResult，保留原Action ID，但不伪造`ProcessActionStateContent`；
3. 以`uncertain_effect`结束为INTERRUPTED；
4. 重复`cancel`或`resume_turn`返回同一终态。

Reducer只在`CANCELLING`、原Process审批存在、Action ID匹配、结果为unknown且有结构化错误时接受这一特殊结果。正常Process终态仍必须携带由权威Action快照生成的私有效果，不能利用取消分支注入成功或失败结论。

取消不会删除PENDING Action、撤销已提交ApprovalRecord、把READY移出队列或宣称RUNNING进程已终止。测试明确证明Session在WAITING_ACTION取消后，原READY Action仍可被Worker执行一次。若产品需要“撤销尚未领取的Action”，必须新增Action Plane原生撤销状态、CAS和审计事件，不能通过修改Session实现。

原Turn截止时间不刷新。Process审批过期后新的客户端决定返回`approval_expired`；重启仍保留Action事实，调用方可取消Session等待或由独立运维流程处理。正常关闭Runtime不会自动取消持久等待；重开后继续使用原边界。

## 6. Worker硬退出与租约UNKNOWN

旧实现把RUNNING/RECONCILING租约过期状态改为UNKNOWN，但未写`ActionResult`；而桥接只允许“终态状态与同状态结果同时存在”，因此Agent无法投影该恢复事实。本片统一SQLite和PostgreSQL行为：

- LEASED租约过期表示执行尚未开始，回到READY且不创建结果；
- RUNNING或RECONCILING租约过期表示副作用可能发生，转为UNKNOWN；
- 同一Journal事务写入`ActionResult(status=UNKNOWN, error.code=lease_expired, retriable=false)`、清理租约并追加`lease_recovered`事件。

真实测试让独立Worker在其子进程启动后`os._exit(89)`，保留仍存活的进程组；租约过期重开后Action和Result均为UNKNOWN，Agent投影unknown并INTERRUPTED，且没有第二模型请求。测试父进程只为夹具清理进程组；生产Runtime仍不会根据持久PID自动清理。

## 7. 跨进程审批竞争

两个独立Python进程可同时读取同一WAITING_APPROVAL并调用`ProcessAgentBridge.decide`。唯一性依赖现有Journal事务，而不是进程内锁：

- SQLite使用`BEGIN IMMEDIATE`串行化状态转换；
- PostgreSQL审批转换使用数据库事务和行级锁；
- 桥接在状态冲突后重新读取Action，再核对完整决定。

相同outcome/actor/reason的竞争者都可幂等返回同一决定，但Journal只有一个`approval_granted`或`approval_rejected`事件。不同决定只有一个获胜，另一方收到`approval_conflict`。Session随后只读取获胜Action决定并补投影，不按到达顺序自创第二决定。

## 8. 实际SDK离线闭环与公开数据

OpenAI `AsyncOpenAI`和Anthropic `AsyncAnthropic`均通过各自SDK与Mock HTTP传输完成同一三步流程：

1. SDK流式返回`host.process`调用，Runtime持久Action并停在审批；
2. 关闭并重开Runtime，提交唯一批准；独立Worker执行固定Python程序一次；
3. 再次重开后观察终态，模型收到有界摘要与Artifact引用，调用`read_artifact(offset=0, limit=1)`读取summary，再完成回答。

所有SSE流都必须关闭，HTTP请求数固定为三次，不重试、不访问网络、不需要真实API Key。模型请求历史只含`outcome/output/error/diff_artifact`白名单；Action ID、Action/批准/绑定指纹、幂等键、私有Process效果和Base64正文不进入wire。模型仅在显式读取Artifact时获得请求页；该测试只读取无正文Base64的summary记录。

## 9. 异常、安全与运维

- Session取消后的Action仍可能执行，客户端必须把INTERRUPTED显示为“等待已停止、外部效果未知”，不能显示“命令已取消”。
- UNKNOWN需要人工核对，不得由队列重试。Effect Journal是判断执行事实的首要来源，Session只用于Agent恢复和展示。
- `lease_expired`不证明进程已停止。生产部署应由容器、服务管理器或未来0.7监督器管理进程组；当前不实现孤儿回收。
- API、Agent与Worker必须部署相同`host.process`工具版本、Principal和宿主绑定；漂移时宁可拒绝恢复。
- Process Artifact可能包含源码或秘密，仍按源码资产保护；scope和摘要不是认证、加密或DLP。
- PostgreSQL只扩展既有恢复写入，不需要迁移；发布前必须在实库CI验证UNKNOWN结果列和并发路径。

## 10. 取舍与后续

本片选择“保守中断但保留权威Action”而不是自动补偿，因为任意命令通常没有可证明的逆操作；选择稳定Saga而不是两阶段提交，因为Session与Effect Journal生命周期和部署角色不同；选择显式`resume_turn`而不是内置轮询，以保持调用预算和宿主调度边界清楚。

0.5.4b2范围至此完成。下一片0.5.4c在同一Process准入和恢复基础上增加固定`git_status`、`git_diff`、`run_tests`及受控命令配置，形成“定位—修改—测试—差异交付”的真实编码闭环。任意Shell、PTY、网络策略、容器Sandbox、宿主死亡监督和多租户认证仍不属于本ADR。
