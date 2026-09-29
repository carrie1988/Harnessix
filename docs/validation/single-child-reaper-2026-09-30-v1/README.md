---
doc_type: validation-evidence
status: current
version: 1
code_revision: 0813c581982fddf17503d47a308419035d193ecf
owners: [core]
modules: [processes, tools, sandbox, evals]
related_adrs:
  - docs/adr/0038-host-process-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/processes/test_runtime.py
  - tests/processes/test_lifecycle.py
  - tests/evals/test_task_pack.py
  - tests/evals/test_engineering_task_pack.py
  - tests/tools/test_windows_git.py
  - tests/integration/test_container_sandbox.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 唯一Child Watcher回收专项验证

## 1. 背景与结论

原macOS CI在超时SIGKILL后出现未知退出255。本机原生3.12实际延迟Watcher并扩大OS终止至回收间隙，
复现原`Transport.kill → Popen.poll`抢先`waitpid`，不是由测试伪造退出码。
后继保持原进程组控制：组终止成功后直接等待Watcher，失败后备只发原组信号，不能调用Popen或新增裸PID控制。
原组失败即使后备回收成功，仍为`cleanup_failed/failed`且实例熔断，不能返回成功。

最终关联回归283项中271通过、12项平台/环境跳过，新5项焦点包含其中且均通过。
6项固定容器/录制Provider实际执行与恢复复验通过，0跳过；不是模型质量、消费者Windows11或完整原生发布验收。
评测及正式Process输入关联290项通过、0跳过。各组含重叠用例，不能相加为独立产品用例总量。
[测试事实](facts.json)绑定最终实际源码与原件SHA；头部Revision为故障/设计基线。

## 2. 根因与最终处理

调用链和状态字段见[完整详细设计](../../changes/m09-r1-single-child-reaper.md)。
保持直接子进程唯一回收，不推断负信号码、不忽略255、不取消内核回收Task。
原程序/cwd/环境/FD、启动新Session、双流限额、超时、取消及原Watcher发布路径不变。

中间直接PID信号候选与完整离线通过结果保留，但**不是最终发布实现**：该方案增加裸PID控制，不采用。
后继重复原组信号候选出现2项回归失败：原组失败的独立后备没有杀死进程，已终止组再次发信号被拒绝。
最终只在原组失败时使用独立OS原组后备，保留失败熔断；成功或已消失的原组直接等待Watcher。
这些失败没有删断言或改变真实退出要求。

Windows原生Job的独立失败是测试启动标记可被取消截断为空。Bootstrap现完整写PID至同目录临时文件，
再Replace发布`started`；原Lease退出、停止原因、双流EOF及真实子进程停止断言不变。
本机POSIX不会执行Windows实现，新的Windows原生结果须独立取得，不能继承旧失败Job的片段。

实际容器在077下先暴露新Workspace模式被收紧为0700，恢复验真正确拒绝；
后继仅对新私有Root内的新目录FD设置正式0755，旧权限漂移仍拒绝且不自动改权。
随后录制Oracle由`git apply`重建为0600，原Patch合同正确拒绝。
录制适配器改为Git可执行位对应的0644/0755，不扩充正式Patch权限、不改真实Provider或评分器。
两类原FAIL及022/077正反例保留，见[完整权限详设](../../changes/m09-r3-eval-workspace-mode.md)。

## 3. 实际测试与材料

| 检查 | 原件结果 | 结论范围 |
|---|---|---|
| 原实现竞争窗口 | 原生Watcher返回255，原4项焦点失败 | 保留失败，不外推所有255的排他原因 |
| 最终新5项焦点 | 通过，含于271项关联通过 | 三种失败后备及原组已消失；实际SIGKILL与唯一回收 |
| Process/Sandbox/正式Process输入 | 271通过、12跳过 | 含原组失败回收与熔断；不是完整产品矩阵 |
| 评测与正式Process输入 | 290通过、0跳过 | 含022/077权限合同、物化恢复和录制Oracle Git模式 |
| 固定容器与Task Pack正式录制链 | 6通过、0跳过，umask077 | 原无网络/只读/资源/Secret约束、Bridge负对照及恢复 |
| 最终资料及可读性治理回归 | 26通过、0跳过 | 关闭中间资料不齐与源码变化引发的两项失败，不改策略或历史基线 |
| 文档与实际Mermaid渲染 | 393文档、10000链接，变化文档45图实际生成SVG | 其余图只计数，不宣称859图全部本轮渲染 |
| Mypy、Schema、全树Ruff及可读性 | 静态检查通过，392源码类型检查 | 不代替OS运行或任务质量 |
| 新Wheel及Secret完整扫描 | 内部1.0.0rc1构建成功，3545输入零命中 | 不创建稳定Tag或商用Release；制品摘要绑定于Facts |

[Facts](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)与
[Manifest](manifest.json)提供计数、开放项和发布文件字节身份。原日志仅保留私有原件，公开内容不复制
用户路径、凭据、模型正文或现有业务容器数据。没有改变固定镜像、Pack、Grader或预算账本。

## 4. 发布与恢复限制

该修正无新依赖、配置或数据库迁移，随原Wheel交付；回退旧实现会恢复相同竞争风险。
中间直接PID候选的完整离线通过不计作最终实现证据；另一轮完整回归在源码与文档仍变化时执行，
文档缺少新证据文件、可读性派生报告与执行时源码不一致两项失败原件保留，不能称为固定候选全绿。
最终固定源码须先完成资料后独立重跑治理检查，并由后继同候选原生矩阵验证。
既有许可证门禁不能称为已通过，也不阻挡功能修复。
真实Suite费用待核对、完整20 Trial质量、默认Desktop路径、消费者Windows11、独立Beta及R1～R6仍开放。
