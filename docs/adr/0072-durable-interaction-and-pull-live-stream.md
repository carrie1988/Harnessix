# ADR 0072：持久交互与Pull-Live事件流

- 状态：已接受
- 日期：2026-09-09
- 实施：0.8.3

## 背景

0.8.2已经提供Headless App Server、Agent SDK、Snapshot和Replay，但客户端只能在命令完成后读取持久事件。真实Coding Agent还需要在Turn运行期间呈现文本、计划、工具进度和Diff，并处理审批、提问、取消及Steering。若把提问或输入队列仅放在连接内存中，服务重启会丢失待决问题；若把文本Delta当成恢复事实，慢客户端和断线会产生无法判定的输出缺口；若stdio仍按“一个请求写入后立即读取一个响应”串行处理，长轮询会阻塞审批和Steering。

## 决策

1. 0.8.3采用`events/next` Pull-Live长轮询，不启用服务端发起的JSON-RPC Request。持久`PublicEvent`和live-only `PublicItemDelta`可在同一响应中返回，但客户端只以`replay.scannedThrough`推进恢复游标。
2. `PublicItemDelta`只优化低延迟显示，不写入Session。App Server每个Thread最多保留1000条，溢出时丢弃最旧Delta并返回`liveGap=true`；客户端必须等待后续`item_finished`持久事件恢复完整正文。未协商`itemDeltas`的连接不得接收Delta。
3. stdio仍保持一个输入Reader和一个输出Writer，但初始化完成后允许最多`maxPendingRequests`个Request并发执行。响应可以乱序完成，SDK由唯一Reader按JSON-RPC id归并到待决Future；长轮询不得阻塞审批、提问答复、取消或Steering。
4. malformed、未知id、EOF和输入流失败必须以稳定SDK错误结算全部相关待决请求。SDK关闭先关闭stdin，使App Server进入有界关闭并唤醒长轮询，再等待或终止子进程；不允许多个协程直接读取stdout。
5. Agent Event/Thread v19增加`WAITING_INPUT`、`QuestionRequestContent`、`QuestionAnswerContent`以及模型重入`reason=steering|context_overflow|normal`。Session migration22登记新投影版本，不改写历史事件。
6. `ask_user`是Runtime显式启用的内建只读工具。模型Tool Call、问题请求和`WAITING_INPUT`先在同一事务提交；答复时问题回答、离开等待状态和成功Tool Result在同一事务提交。问题、Call、Turn和Thread身份必须全部匹配。
7. 待决提问跨重启保持`WAITING_INPUT`，不会自动调用Provider或工具。答复后的`EXECUTING_TOOLS`只有在已经存在配对成功Tool Result时才是安全恢复边界；超出原Turn墙钟预算后，答复失败，显式恢复或Runtime重开把Turn结算为`time_budget_exceeded`。
8. Steering以`threadId + turnId + requestId + text`绑定当前活动Turn，正文作为持久`user_message` Item提交。当前Provider调用不被中断；当前模型步骤和Tool Result先完成，Steering随后进入下一模型步骤。Steering可在`ACCEPTED`及运行/等待边界提交，在`FINALIZING`、取消中和终态失败关闭。
9. 模型步骤结束与Steering检测必须在同一Thread锁和CAS提交内完成，避免“先判定无输入、再提交终态”造成输入丢失。若Steering早于当前步骤首个模型Item到达，Reducer仍把当前步骤模型输出及Tool配对放在该Steering之前。
10. `artifact/read`只在显式装配`ScopedProtocolArtifactReader`时广告。读取先由Session获得Thread Workspace，再经`ArtifactAccessScope`取得当前Workspace能力，并按`threadId + workspaceScope + artifactId`读取有界页面；客户端不能提交作用域或直接访问SQLite。
11. 薄CLI只能调用`AgentClient`，不导入Session、Runtime或执行端口。CLI支持创建、列表、恢复/跟随、运行、重试、分叉、归档、Steering和取消；审批前先分页显示Diff Artifact，提问支持编号选项。完整TUI、终端布局和发行体验仍属于0.9。
12. 已发布的Cost Report v1/v2、Smoke和Campaign Schema继续使用冻结的v18 Turn状态集合。出现`WAITING_INPUT`时生成Cost Report v3，避免向历史Schema静默加入枚举值。

## 失败语义

| 场景 | 结果 |
|---|---|
| 问题请求提交后进程退出 | 重开仍为`WAITING_INPUT`，不重复问题或Tool Call |
| 问题答复事务提交后、后台调度前退出 | 相同Command返回原结果；`thread/resume`从配对Tool Result后继续模型 |
| 重复回答相同内容 | 返回原Turn，不追加事件 |
| 重复回答不同内容 | `question_conflict` |
| 问题、Call、Turn或Thread错配 | `question_not_found`、`question_mismatch`或既有归属错误，均不提交回答 |
| 等待输入期间取消 | 提问对应Tool Result结算为取消，Turn进入`CANCELLED`；迟到回答为`question_closed` |
| Steering与模型完成竞态 | Thread锁内二选一提交`PREPARING_CONTEXT(reason=steering)`或`FINALIZING` |
| Delta缓冲溢出 | 丢最旧Delta、返回`liveGap`，以持久`item_finished`恢复 |
| 长轮询占用stdio连接 | 其他Request并发处理，Response按id乱序归并 |
| Response id未知或JSON损坏 | SDK以`invalid_response`结算待决请求，不猜测对应关系 |
| CLI在命令响应后退出 | 重开后从游标0重建待决交互投影，再从已保存游标继续显示 |

## 取舍

- Pull-Live比服务端Request少一套待决请求和反向超时协议，适合当前单客户端stdio边界；代价是最多50毫秒轮询观察不产生文本Delta的持久事件。真正的服务端Request仅在出现多客户端或远程传输需求后另行设计。
- 当前`ask_user`一次只提交一个问题和一个字符串答案；多问题、多选、Secret输入和自动决议需要新的显式合同，不能扩张v19语义。
- Steering不抢占Provider，保证模型尝试账本和Usage完整；因此它不是低延迟中断。需要立即停止时客户端应使用`turn/cancel`。
- live Delta不提供exactly-once承诺，换取Session不按Token碎片膨胀。任何需要审计的最终文本必须来自持久Item。

## 验证

- 提问等待、重启、回答、重复/冲突、取消、过期和硬退出恢复；
- 运行中及`ACCEPTED` Steering、首个模型Item前竞态、关闭Turn拒绝和模型历史顺序；
- Delta、Replay、超时、能力协商、1000条溢出和`liveGap`；
- 子进程乱序Response、malformed Response、stdio长轮询与并发请求；
- CLI快速终态回放、提问选项以及真实整组Patch Diff读取后审批；
- Agent Event/Thread v19、Cost Report v3、migration22和历史Schema哈希。
