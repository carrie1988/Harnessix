---
doc_type: adr
status: current
version: 2
code_revision: 532e59b346f50657518d11225102bc6999c301e6
owners:
  - core
modules:
  - product_config
  - product_ui
  - tools
  - workspace
related_adrs:
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0065-platform-capability-ports-and-execution-plan.md
  - docs/adr/0075-provider-profile-secret-and-safe-fallback.md
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
related_tests:
  - tests/product_config/test_runtime.py
  - tests/product_config/test_preflight.py
  - tests/product_config/test_wizard.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_ui/test_cli.py
  - tests/tools/test_runtime.py
  - tests/tools/test_windows_read_adapter.py
  - tests/tools/test_windows_native_runtime.py
  - tests/workspace/test_snapshot.py
supersedes: []
---

# ADR 0079：只读产品诊断与原生Workspace读取端口

## 状态

接受并已由实现提交`532e59b346f50657518d11225102bc6999c301e6`落地；本地合同、失败恢复和结构治理已通过，
Windows原生与全矩阵CI完成前保持0.9.1d候选状态。完成证据见
[0.9.1d详细设计](../changes/m09-1d-configuration-preflight-windows-read.md)和现行模块文档。

## 背景

0.9.1c关闭后，终端产品已经能够恢复会话并处理完整领域交互，但首次配置和Windows默认启动仍未形成产品闭环：

- Product Config只能手写或迁移，没有安全向导；
- 配置诊断只覆盖Provider/Profile/Secret，不覆盖Workspace、状态、TUI和平台端口；
- TUI在子进程失败后才看到大部分启动错误；
- Tools `Workspace`直接依赖POSIX FD；
- Windows已有Win32 Handle Workspace端口，却未接入`CodingToolRuntime`；
- `agent-server`通过平台门失败关闭。

[专项源码研究](../research/configuration-preflight-and-windows-read-runtime.md)表明，成熟产品会把Doctor事实与人类渲染
分离、保持Doctor只读，并为平台路径保留原生实现。Harnessix不能通过移除平台门、使用普通`Path.resolve`或要求WSL
来宣称Windows支持。

## 决策驱动因素

1. 首次配置不能保存API Key明文；
2. 启动前必须给出稳定、脱敏、可操作的阻断原因；
3. Doctor必须安全、可重复、可用于支持诊断；
4. Preflight后的状态仍可能变化，Server必须二次校验；
5. macOS/Linux现有POSIX安全语义和工具版本不能被无关重写破坏；
6. Windows必须使用现有Handle链、File ID和Reparse拒绝能力；
7. Agent和TUI只能依赖统一Tool合同，不感知平台分支；
8. 取消、5秒Deadline、分页Revision、结果预算和关闭语义必须跨平台一致；
9. 无等价安全实现的可选能力必须不广告，而不是弱化。

## 候选方案

| 方案 | 满足的驱动因素 | 不满足的驱动因素 | 成本/风险 |
|---|---|---|---|
| A. 删除平台门并直接复用POSIX Tools | 改动最少 | Windows API不可用，破坏2/6/8 | 启动即失败或产生路径竞态 |
| B. Windows要求WSL | 快速复用POSIX | 不满足原生1.0承诺 | 路径、凭据、终端和文件身份分裂 |
| C. 用`Path.resolve`重写跨平台Walker | 表面统一 | 无句柄链、无法防TOCTOU | 安全保证显著回退 |
| D. 保留POSIX实现，新增基于现有Win32端口的Tools Adapter | 满足全部核心因素 | 两个原生适配器需要共同合同测试 | 选定 |
| E. 先实现统一抽象并重写POSIX/Windows两端 | 长期形态整洁 | 影响面过大，0.9.1d回归风险高 | 拒绝；只提取必要最小端口 |

## 决策

采用以下不可违反的分层与不变量：

1. 新增版本化`ProductPreflightReport`，作为Preflight、Doctor、JSON和人类渲染的唯一事实；
2. `doctor`只读取和探测，不写配置、不激活Profile、不创建Thread、不联网；
3. `configure`是唯一新增配置写入口，只写Secret引用，不接收或保存Secret值；
4. 配置创建使用私有目录、锁、原子替换和目录同步；替换已有文件必须提供源SHA-256 CAS；
5. `code`启动在打开Client State和子进程前运行Preflight；不Ready时不进入TUI；
6. `agent-server`保留独立复核，Preflight不是授权凭据，也不消除TOCTOU；
7. `CodingToolRuntime`按宿主选择POSIX FD端口或Windows Handle端口，对外继续使用现有ToolDescriptor及输入输出合同；
8. Windows端口复用`WindowsWorkspaceRoot`和公共路径规范化，不复制普通Path Walker；
9. Windows文件、目录、Glob和Grep必须保留拒绝路径、预算、Revision、取消、Deadline、Artifact和关闭语义；
10. Windows没有等价受管Git进程端口前不广告`git_status/git_diff`；显式请求Windows Git时稳定失败；
11. 未知平台继续失败关闭；任何原生端口探测失败都不能降级到字符串路径实现；
12. 0.9.1d只形成原生**只读**产品链，不扩大写入、Process、Delivery或Sandbox授权。

## 理由

### 1. 单一诊断事实避免分叉

如果CLI、TUI和Server各自产生一套检查文本，同一环境会出现不同结论。版本化报告先固化稳定Code、阻断级别、状态、
修复动作ID、时长和摘要，再由适配器渲染，可以直接用于自动化、支持包和回归测试。

### 2. Doctor只读才能成为可信证据

自动修复会改变配置和状态，使“检查结果”无法说明修复前还是修复后的事实，也扩大误改风险。显式Configure写路径可单独
使用CAS、收据和失败恢复；Doctor只给动作建议。

### 3. 原生端口优于虚假统一

POSIX FD和Windows Handle解决的是同一对象安全问题，但系统调用、共享和路径身份不同。统一的是上层Contract和失败语义，
不是底层API。复用已经验证的Windows端口可以避免新建不受审计的路径解析器。

### 4. 能力缺失应体现在广告目录

Windows文件读取安全完成不代表Git进程执行安全完成。Agent只应看到当前宿主真正装配的Tool；不广告比调用时偷偷使用
弱实现更安全，也保持模型历史和审批Fingerprint真实。

## 后果

### 正面后果

- 首次用户可以在不写入Secret值的情况下生成严格配置；
- 启动错误在TUI前以稳定报告暴露；
- Doctor可离线、安全重复运行；
- Windows进入真实默认只读产品链；
- Agent、Protocol和UI无需平台分支；
- 现有POSIX实现保持局部稳定；
- 未来安装诊断、支持包和TUI Doctor Screen可复用同一报告。

### 负面后果与债务

- Tools内部存在POSIX和Windows两个读取适配器；必须用共享合同防漂移；
- Windows递归搜索需要基于Handle观察实现独立遍历，增加平台测试成本；
- Windows Git Tool仍不可用，必须明确能力目录和错误；
- Doctor报告是瞬时事实，不作为启动授权；
- Windows状态目录ACL、安装器签名、长期终端稳定性和写入交付仍由后续切片关闭。

## 兼容、安全与运维影响

- Agent Protocol保持`1.0`，Session与Client State Schema不变；
- 新Preflight合同新增Schema，不修改`ConfigurationDiagnosticReport v1`；
- POSIX Tool输入输出Schema保持不变；平台实现摘要进入Tool Version，因此不同端口不会共享错误Fingerprint；
- `harnessix code WORKSPACE ...`保持兼容；新增`harnessix code doctor`和`harnessix code configure`；
- 配置替换需要显式源摘要，旧配置不会被向导静默覆盖；
- Doctor输出禁止Secret、Prompt、原始异常、完整环境和用户绝对路径；
- Server继续在stdio前校验配置、目录重叠和原生端口；
- Windows Git显式参数在支持前返回稳定失败，不影响默认四项文件/搜索工具。

## 验证方式

1. 配置草案、生成配置、创建、重复创建、CAS替换、崩溃窗口、链接和权限测试；
2. Preflight顺序、Ready计算、跳过语义、脱敏、单项异常隔离和稳定摘要测试；
3. Doctor JSON与人类渲染来自同一报告，退出码与阻断状态一致；
4. 启动在Preflight失败时不创建Client State、不启动子进程；
5. POSIX现有Tools全量回归；
6. Windows原生List/Read/Glob/Grep的Unicode、空格、长路径、分页、取消、超时和Artifact测试；
7. Windows Junction、ADS、设备名、硬链接、大小写冲突、对象替换和根替换攻击测试；
8. Windows真实`agent-server` + SDK纵向读取与关闭测试；
9. Linux Python 3.12/3.13、macOS和Windows CI矩阵全部通过；
10. 全量`make check`、文档门禁和真实Mermaid渲染通过。

## 关联资料

| 类型 | 路径/链接 | 关系 |
|---|---|---|
| 源码研究 | [配置、Preflight、Doctor与Windows原生只读Runtime研究](../research/configuration-preflight-and-windows-read-runtime.md) | 固定参考事实与采用边界 |
| 重大变更设计 | [0.9.1d详细设计](../changes/m09-1d-configuration-preflight-windows-read.md) | 接口、流程、测试和回退 |
| 总体设计 | [0.9.1总体详细设计](../changes/m09-1-cli-tui-product-experience.md) | 子切片上下文 |
| 现行模块 | [Product Config](../modules/product-config.md)、[Product UI](../modules/product-ui.md)、[Tools](../modules/tools.md)、[Workspace](../modules/workspace.md) | 实现后当前事实源 |
| 平台 | [平台与运行环境](../operations/platforms.md) | 支持声明与剩余边界 |
| 测试 | [`tests/product_config`](../../tests/product_config/)、[`tests/product_ui`](../../tests/product_ui/)、[`tests/tools`](../../tests/tools/)、[`tests/workspace`](../../tests/workspace/) | 合同与平台证据 |

## 被取代关系

本ADR不取代ADR 0063、0065、0075或0078，而是把其Windows、平台端口、配置安全和产品启动原则收敛为0.9.1d
可执行决策。后续若统一原生工具端口或引入在线Preflight，必须以新ADR明确取代对应条款。

## 实现复核（532e59b）

实现保持本ADR的边界，并形成两条经独立评审的新一级包依赖：

- `product_ui -> product_config`：Configure、Doctor和Startup只消费产品配置合同/应用服务；没有反向依赖；
- `tools -> workspace`：WindowsReadPort只消费原生Handle观察端口；Workspace不导入Tools实现，没有形成环。

公共API新增Draft、Write Receipt、Preflight Check/Report/Request、Builder、Writer和Preflight入口；基础v2配置、Tool输入输出、
Agent Protocol、Session与Client State合同未变。可读性复核将新职责拆为小文件/小函数，没有批准新的超大文件或高复杂度符号。
上述依赖和公共API已写入[`readability-policy-v1.json`](../../governance/readability-policy-v1.json)，其批准理由是本ADR，而非
以更新快照规避门禁。原生Windows测试和CI URL仍需在候选关闭时回填。
