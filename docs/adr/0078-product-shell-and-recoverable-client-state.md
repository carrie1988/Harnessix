---
doc_type: adr
status: current
version: 2
code_revision: d9dbfe664a14d7095e2c4adbfd1b2c88f4d4c5c6
owners:
  - core
modules:
  - cli
  - tui
  - sdk
  - app_server
  - product_config
related_adrs:
  - docs/adr/0009-app-server-protocol.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0070-agent-protocol-v1-boundaries.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0072-durable-interaction-and-pull-live-stream.md
related_tests:
  - tests/app_server/test_agent_cli.py
  - tests/app_server/test_server_sdk.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# ADR 0078：产品终端壳与可恢复客户端状态

- 状态：已接受，等待0.9.1分片实现
- 日期：2026-09-13
- 决策范围：Harnessix Code 0.9.1

## 1. 背景

Harnessix当前薄CLI已经能够通过Agent Protocol创建和恢复Thread、运行Turn、消费持久Replay与Live Delta、
回答审批和问题并读取Diff Artifact。它仍要求操作者手工提供App Server argv、客户端UUID和每次Command ID，
也不保存当前Thread与持久Cursor。SDK存在Response校验、单帧上限、Result错误归一和半握手恢复缺口。

如果直接在这些边界上增加TUI，连接失败或进程重启会使界面无法判断命令是否已经被服务端接受，Hydration与
Live事件也可能按Task完成顺序覆盖。完整产品需要先确定界面、应用控制器、客户端恢复状态、SDK和App Server
之间的权威边界。

固定源码证据及框架比较见[CLI/TUI产品体验研究](../research/cli-tui-product-experience.md)。

## 2. 决策

### 2.1 产品分层

采用四层产品客户端结构：

1. **Textual View**：只负责终端输入、布局、Screen/Modal、渲染和无头UI测试；
2. **Product Controller**：接收类型化UI Intent，串行化命令副作用，管理前后台任务和退出顺序；
3. **Projection与Recoverable Session**：以Snapshot、持久Replay和Live Delta生成不可变视图，管理连接代际；
4. **Agent SDK**：执行严格Agent Protocol交换，不拥有终端状态或产品Policy。

Widget不得直接调用Transport、生成Command ID、推进持久Cursor或修改Agent领域状态。Agent Runtime、Session
Store和Protocol仍是Thread/Turn/Item/Approval/Question的权威事实源。

### 2.2 最小客户端持久状态

在产品状态目录保存版本化`ClientState`，只包含：

- 跨进程稳定的`client_instance_id`；
- 单调`next_command_sequence`；
- Workspace指纹与最近选择的`thread_id`；
- 每个Thread最后确认的`durable_cursor`；
- 状态Schema版本和最近安全关闭标志。

状态文件不保存Transcript正文、Diff正文、Approval/Question答案、Provider响应或Secret。写入使用同目录临时文件、
flush/fsync、原子替换和目录同步；并发进程通过现有跨平台文件锁模式实现单写者。未知Schema、符号链接、过宽权限、
Workspace指纹错配和内容损坏均失败关闭或隔离后显式重建，不静默猜测。

Command ID在请求发送前分配并持久化，格式由客户端实例与单调序列确定。请求结果不明确时，同一逻辑操作必须复用
原ID查询或重放；只有操作者显式发起新操作才分配新ID。

### 2.3 恢复与投影

`ProductViewState`只通过纯`ProjectionReducer`更新：

1. 获取Thread Snapshot；
2. 进程冷启动从Cursor 0分页Replay完整历史；当前`ThreadView`不含Item，已保存Cursor不能替代Transcript快照；
3. 同一进程内存投影完整的暖重连可以从该投影已确认的Cursor续传；
4. 原子应用一页事件并持久化`scanned_through`；冷启动重建到已保存Cursor之前不得进入Live；
5. 进入`events/next`获取持久事件和Live Delta；
6. Live Delta只影响临时展示，不推进持久Cursor；
7. `item_finished`持久正文覆盖临时Delta并消除Gap；
8. 连接重建后丢弃旧连接代际迟到结果，再从持久事实Hydrate。

Reducer输出包含Transcript、当前Turn、计划、工具状态、待决交互、Usage/Cost和连接状态，但不执行I/O。相同
Snapshot和有序事件序列必须产生逐字段相等的视图。若未来增加服务端完整Transcript Snapshot，必须通过新协议
合同后才能把冷启动起点从0改为Snapshot声明的覆盖Cursor。

### 2.4 生命周期与取消

所有SDK和子进程I/O在Controller拥有的受管异步任务中执行。退出TUI、取消一个UI Worker、取消当前Turn和
终止App Server是不同操作：

- 关闭Modal只取消本地展示任务；
- `turn/cancel`必须由显式Intent生成持久Command；
- 退出时先停止接收新Intent，再取消长轮询，等待在途Command达到有界结算点，最后关闭Client和Renderer；
- 超时只形成`outcome_unknown`或稳定传输错误，不在UI层假定领域命令失败；
- 终端恢复在`finally`路径执行，清理失败不得覆盖更重要的领域/传输错误，但必须进入诊断记录。

### 2.5 渲染框架

选择Textual 8.x作为TUI渲染层，并在0.9.1初始实现中约束为`>=8.2,<9`。Textual通过`tui`可选依赖隔离纯
Server/SDK安装；正式Harnessix Code终端产品安装物必须包含该Extra。缺少依赖时，CLI返回稳定错误和静态安装
指引，不动态联网安装。

Controller、Store、Reducer、View Model和错误目录不导入Textual，确保协议/恢复语义可由普通Pytest测试。
Textual测试使用`App.run_test()`与Pilot验证按键、焦点、尺寸、Modal和退出；不依赖真实TTY或Snapshot人工目测。

### 2.6 启动检查和错误自助

启动检查分两类：

- **安全关键**：配置合同、状态目录私有性、Workspace隔离、Secret引用、平台能力和协议协商，失败时不开启
  Agent交互；
- **体验类**：终端尺寸、颜色、可选Git和非关键能力，失败时进入受限界面并展示可执行自助动作。

错误展示只消费稳定错误目录：错误码、分类、可重试性、影响范围、建议动作和可安全展示的上下文。原始stderr、
模型正文和Secret不直接进入错误Modal。

### 2.7 Windows与统一Action

Windows是0.9.1验收平台，不以WSL替代。默认只读Coding Tool必须通过独立Windows Workspace安全端口处理盘符、
UNC、大小写、保留名、ADS、Reparse Point/Junction和共享模式；不得移除当前平台检查后复用POSIX路径实现。

Patch、Process、Delivery、Artifact和扩展只能通过既有Trusted Action、Policy、Approval、Effect Journal与
Reconcile边界装配。TUI不增加旁路文件写入、任意Shell或“始终允许”权限。完整装配按0.9.1独立子切片完成，
每一类能力未通过故障与三平台测试前不得出现在默认广告目录。

## 3. 备选方案

### 3.1 继续扩展行式`ThinAgentCLI`

未采用。它适合协议验收和自动化诊断，但难以可靠处理并发流、Modal焦点、Diff布局、会话导航和终端恢复。薄CLI
继续保留为Headless/回归入口，不承担完整产品体验。

### 3.2 让Textual Widget直接调用`AgentClient`

未采用。重绘、焦点切换和组件卸载会把I/O生命周期分散到多个Widget，难以保证Command只发送一次、连接代际
隔离和退出清理顺序。

### 3.3 把Transcript复制到本地UI数据库

未采用。Session Store已经是持久事实源；第二份正文会引入迁移、隐私、清理和一致性问题。客户端只保存恢复
元数据，通过Snapshot/Replay重建视图。

### 3.4 使用prompt_toolkit或手写ANSI

暂不采用。两者可以实现目标，但Screen/Modal、Worker生命周期、跨平台输入和无头测试需要更多自建基础设施。
如果Textual Major升级无法满足兼容或性能要求，可在保持Controller/Reducer接口不变的前提下替换View层。

### 3.5 只在POSIX发布，Windows继续依赖WSL

未采用。ADR 0063已把Windows列为1.0支持平台；平台安全语义不能由兼容层宣传替代。

## 4. 后果

正向后果：

- UI故障不会改变Agent领域事实；
- 重启、断线和Hydration竞态有确定恢复路径；
- 命令幂等身份不再由操作者手工生成；
- View框架可替换，核心恢复逻辑可快速、确定性测试；
- Windows与高风险工具装配有明确阻断条件。

代价与约束：

- 新增本地客户端状态Schema、迁移、锁和安全维护责任；
- 引入Textual依赖及Major版本兼容测试；
- 0.9.1必须拆为多个纵向子切片，不能用单次大提交完成；
- 本地状态损坏后的保守恢复可能要求重新选择Thread，但不会伪造旧命令结果。

## 5. 验收条件

1. SDK严格校验Response Envelope、帧预算和Result，并能从半握手失败重建连接；
2. ClientState在macOS、Linux、Windows原子持久化，锁冲突、崩溃窗口、损坏和迁移有测试；
3. Command在发送前获得持久ID，断线重试不重复领域命令；
4. Snapshot/Replay/Live/Gap/迟到连接事件由纯Reducer确定归并；
5. Textual TUI覆盖会话、流式输出、计划、工具、Diff、审批、Question、Usage/Cost、取消、Steer和错误自助；
6. 配置向导和Preflight不会回显Secret，安全关键检查失败关闭；
7. Windows原生默认只读工具链通过对象安全和产品启动验收；
8. 默认统一Action装配不绕过Policy、Approval、Journal、Sandbox和Reconcile；
9. 三平台CI、无头UI、真实stdio、故障恢复和文档门禁全部通过后，路线图0.9.1才可关闭。

## 6. 实施与追踪

完整接口、数据、时序、测试和回退计划见
[0.9.1 CLI/TUI产品体验详细设计](../changes/m09-1-cli-tui-product-experience.md)。实现完成前，现行能力仍以
[SDK模块](../modules/sdk.md)、[App Server模块](../modules/app-server.md)、
[Product Config模块](../modules/product-config.md)、[Tools模块](../modules/tools.md)和
[总体架构](../architecture.md)为准。
