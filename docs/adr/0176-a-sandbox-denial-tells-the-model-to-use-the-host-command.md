---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [trusted_actions]
related_adrs:
  - docs/adr/0111-native-process-sandbox-backends.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
related_tests:
  - tests/trusted_actions/test_sandbox_denial.py
  - tests/product_config/test_native_sandbox_action.py
  - tests/product_config/test_native_host_command.py
supersedes: []
---

# ADR 0176 沙箱拦截时提示模型改用沙箱外命令

- 状态：评审中
- 日期：2026-11-18
- 修订：命令非零退出的英文反馈（`agent_gateway_output._process_failure_feedback`）

## 背景

macOS Seatbelt与Linux bubblewrap里的命令按设计不能写HOME等工作区以外的位置，也不能联网（ADR 0111）。Beta Issue #44修复了
`mktemp -d`之后，Java项目的Maven Wrapper首次运行仍在创建`~/.m2`一步失败（报告人复测：公网冷下载在沙箱里失败，改用本地镜像
才通过）。模型只看到普通的非零退出说明（“命令运行过并以非零状态退出”），不知道这是沙箱边界。系统说明里写了“需要联网或
写工作区以外时改用host_command”，但报告的任务提示禁止其他命令，模型没有改用。报告的第二项期望是：首次下载需要宿主或
网络批准时，给出可照着做的后续提示。

负责人2026-11-18选方案A（另有B：沙箱里的每次非零退出都提示；C：只写文档）。

## 决策

1. **何时提示。** 只在同时满足时：计划的沙箱级别为`host_sandboxed`（Seatbelt或bubblewrap）；结果为`process_nonzero_exit`；
   诊断预览的stdout或stderr里有明确的拦截迹象
   （[`sandbox_denial.DENIAL_SIGNATURES`](../../src/harnessix/trusted_actions/sandbox_denial.py)：写入被拒的
   `Operation not permitted`、`Read-only file system`，以及域名解析失败、网络不可达等措辞，不区分大小写）。
   不含`Permission denied`与`Connection refused`：普通失败里也常见，不能说明是沙箱所致。
2. **提示内容。** 在原有非零退出说明后追加一句英文：输出显示沙箱拦截了命令，沙箱里的命令不能写工作区以外（如首次下载
   构建工具或依赖时写HOME），也不能联网；确实需要时改用沙箱外运行的命令工具（若提供），它会请用户批准；不要用其他方式
   绕过沙箱。不写具体工具名：Trusted Action层不依赖产品的工具命名，系统说明已介绍`host_command`。
3. **只读已校验的预览。** 判断只用刚通过Owner、审计Hash、Secret与合同校验的投影，不读取存档或原始日志。

## 没有改变的

- 沙箱边界、网络策略与审批规则不变；提示不授予任何权限，改用沙箱外命令仍需用户批准。
- 沙箱外命令（`host_guarded`）、容器Profile与外部边界Profile不加提示；超时与其他失败的说明不变。
- 界面按错误码显示固定文案，不读这段英文。

## 后果

- 首次运行Maven/Gradle Wrapper、安装依赖等因沙箱失败时，模型得到可照着做的下一步；普通测试失败不受影响，避免模型动辄申请
  沙箱外执行（审批次数与耗时）。
- macOS与Linux相同；Windows没有本机沙箱（命令逐条审批），不涉及。

## 验证

- `tests/trusted_actions/test_sandbox_denial.py`（三个平台）：每种拦截措辞在stdout或stderr里都触发提示，报告中的
  `mkdir: …/.m2: Operation not permitted`触发；断言失败、`Permission denied`、`Connection refused`、空输出不触发；
  `host_guarded`与`container_strong`不触发；预览缺失或不可显示时不触发。
- `tests/product_config/test_native_sandbox_action.py`（真实Seatbelt或bubblewrap：247以nobody运行，macOS须在普通终端）：
  沙箱里`mkdir -p "$HOME/.m2/wrapper"`失败时说明带提示、HOME未被写入；普通失败不带提示。修改前没有提示。
- `tests/product_config/test_native_host_command.py`：沙箱外命令输出同样的措辞也不带提示。
