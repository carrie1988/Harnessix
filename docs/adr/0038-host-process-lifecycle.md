# ADR 0038：受信宿主进程生命周期与有界捕获

- 日期：2026-09-05
- 基线：`76dae11`，CI33893001258四项通过，开工fetch一致
- 状态：已采纳，0.5.4a范围已实现并完成本地验收；不将基础层称为已完成整个0.5.4

## 1. 源码与接口求证

研究提交与0.2冻结基线相同，不复制实现：

- Codex `a0dcfe2` 的 `codex-rs/utils/pty/src/pipe.rs`：独立子会话、清空环境、独立双流读取和组终止。`process_group.rs` 的Linux parent-death信号与macOS组成员后备方案说明跨平台清理不是简单kill PID。不能将这些机制一概算作Python基础层已实现。
- OpenCode `69c172e` 的 `packages/opencode/src/tool/shell.ts` 与 `packages/core/src/process.ts`：取消与截止时间竞速，组执行和有界流捕获。旧 `packages/opencode/src/util/process.ts` 的环境合并/完整buffer不作为本项目默认值；同仓库不同路径不能混为一种语义。
- Claude Code辅助仓库 `2ca5dda` 的 `src/tools/BashTool/BashTool.tsx` 包含后台任务切换；该非官方仓库仅辅助比较，本片不增加后台执行或自动续跑。
- Python官方[Subprocess](https://docs.python.org/3.12/library/asyncio-subprocess.html)说明单纯wait可能因管道阻塞而死锁，communicate缓冲不适合无限输出；[Protocol/Transport](https://docs.python.org/3.12/library/asyncio-protocol.html)提供独立process_exited、pipe_connection_lost和管道transport关闭接口。本片使用这些公开API，不读私有`_transport`或StreamReader缓冲。

## 2. 本片交付及非目标

新增宿主专用 `HostProcessRuntime`，构造时绑定绝对cwd、命名可执行文件表、显式环境与资源策略。调用只给出已注册程序名、argv和可缩短的超时；不接受shell字符串、任意cwd或模型提供的环境。不搜索PATH选择主程序，不继承父进程环境或stdin，关闭额外FD，POSIX新会话/进程组。一次仅一个执行，忙时明确拒绝，不隐藏排队或刷新截止时间。

启动环境允许列表约束传给exec的映射，不约束程序初始化后的自改环境。本片macOS CI曾因把子进程环境键集当作启动映射而失败；本地CPython 3.12.8 Framework以仅NO_COLOR的环境启动仍生成`__CF_USER_TEXT_ENCODING`，3.12.7 Anaconda未生成。Apple公开 [CFRuntime初始化](https://github.com/apple-oss-distributions/CF/blob/main/CFRuntime.c)调用默认编码初始化并注明可能设置环境。修正验收为真实启动边界的精确映射断言、敏感哨兵不继承和子进程初始化行为分别校验，不将该变量加入宿主可配置环境列表。

可执行文件和cwd记录身份并在运行前复核。这是变化检测，不是原子exec/CAS或路径隔离；程序、解释器、参数和仓库代码仍须受宿主信任。进程可访问宿主权限范围，不把cwd当Sandbox，不运行用户未批准的不可信仓库代码。不新增模型工具、Kernel/Session审批事实、Process Journal、Artifact或迁移；Agent v8、Session migration9、Provider v3、副本v3及原工具定义不变。

## 3. 输出与终态

双流在独立协议回调中持续接收，每流只保留有界原始字节前缀，另计数和散列实际观察字节。以规范Base64表示，提供严格UTF-8解码，跨块/截断/二进制不替换字符冒充原文。分别记录前缀截断与自然EOF；未EOF的摘要只是已观察前缀，绝不是全输出摘要。stdout/stderr各自有序，不宣称跨流全序。

展示上限不会停止正常排水。另有共享输出停止阈值，达到后关闭仍打开的管道并请求终止；最后一个已交付块可越过阈值，不是内核输出配额，关闭后不继续无限散列。强制关闭不伪装自然EOF。

返回码（含负信号号）、停止原因和组信号记录互相独立。零退出不代表测试通过、没有副作用或全部后代安全终止。非零退出仍保留双流。无法发送组信号时明确cleanup_failed，不静默成功。

## 4. 取消、退出与回收

从调用准入时计算单调截止时间。正在创建的进程不能因取消协程而丢失句柄；内部生命周期不受调用方Task直接取消，取得句柄后进行组终止及回收。Token取消返回已启动进程的cancelled结果；Task取消/外部wait_for先排空再传播原取消；启动前取消不创建进程。close拒绝新执行，停止当前执行，支持重复close及重复Task取消。

主进程退出与所有管道EOF独立等待。即使主进程已退出或后代关闭了双流，仍清理本次进程组。先TERM、宽限后必要时KILL，等待直接子进程的退出回调（asyncio已wait/reap），再有界等待管道排空；超过管道等待期限强制关闭读端并标记非EOF，不永久等待被脱组后代持有的FD。

进程组不是完整树容器；后代setsid/setpgid脱组、宿主SIGKILL/崩溃、不可中断内核状态不能保证整体终止。正常可终止进程的超时包含终止宽限与排水时间；创建/内核回收不宣称硬实时上限。既不按历史PID重启杀进程，也不宣称此片已有宿主硬崩溃恢复。更强containment/宿主死亡处理与持久执行准入须在后续单独设计验收。

## 5. 验收门禁及后续

实际临时进程验证双流填满、截断、二进制、EOF、非零/信号退出、环境/FD隔离、argv不解释Shell、截止时间、Token/Task取消、重复取消/关闭、启动窗口、主进程提前退出、忽略TERM、同组孙进程和脱组管道持有者。故障测试必须清理自身创建的所有测试后代，不能为了展示越界留下后台进程。

0.5.4b再设计持久进程意图/审批/结果及未知效果：启动前后进程崩溃不自动重放，模型侧不是普通READ_ONLY工具。0.5.4c在该准入上接Git与run_tests，再以0.5.5真实缺陷修复Eval验证读取→编辑→运行测试→修正→实际Diff交付。测试框架名称不赋予执行安全性。
