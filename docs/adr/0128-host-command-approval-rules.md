---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product_ui, product_config]
related_adrs:
  - docs/adr/0111-native-process-sandbox-backends.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
related_tests:
  - tests/product_config/test_command_rules.py
  - tests/product_ui/test_approval_rules.py
supersedes: []
---

# ADR 0128 沙箱外命令的免询问规则

- 状态：评审中
- 日期：2026-11-11
- 修订：[ADR 0113](0113-sandboxed-command-approval-and-host-escalation.md)第2.2节“审批：每条都要用户批准……没有任何自动批准路径”

## 背景

Windows没有本机沙箱（ADR 0111、用户决定W2），所有命令都是沙箱外命令，每条都要批准；macOS、Linux在沙箱不可用时
同样如此。用户决定（2026-11-11）：Windows不做操作系统级沙箱，保持“审批 + Job Object + 原生文件检查”，并参考
Claude Code在原生Windows上的做法减少不必要的询问——只读命令不询问，其余命令可以“以后不再询问”。范围取核心版，
三个平台统一，规则保存在本机工作区状态里。

逐条询问的代价是审批疲劳：用户连`dir`、`git status`都要点一次，很快就会不看内容直接批准，审批失去意义。

## 决策

1. **服务端策略不变。** 沙箱外命令仍然每条产生审批记录；`exec --approval-mode auto`仍不覆盖它，无人值守运行和评测不会
   在无人知情时执行无隔离命令。自动批准发生在用户自己的交互客户端（终端界面），并把原因写入审批记录。
2. **只在命令文本能被可靠判断时免询问。** 复合命令（`|`、`;`、`&&`、`&`）、重定向、子表达式、变量、转义字符和非ASCII
   字符一律询问。解析不了就询问。
3. **内置只读命令不询问。**
   - POSIX：`ls`、`pwd`、`stat`、`du`、`tree`、`which`、`echo`（只输出名称与元数据），`cat`、`head`、`tail`、`wc`、
     `diff`、`file`（输出文件内容，选项从严、不允许通配符）。
   - PowerShell：`Get-ChildItem`、`Get-Location`、`Test-Path`、`Get-Item`、`Resolve-Path`、`Get-Command`、
     `Write-Output`及常用别名，`Get-Content`、`Get-FileHash`及别名。
   - Git：`status`、`diff`、`log`、`show`、`rev-parse`、`ls-files`；不允许全局选项，不允许`--output`、`--ext-diff`、
     `--no-index`。
4. **免询问读到的内容不多于读工具。** 路径参数必须是工作区内的相对路径，并沿用读工具的敏感名称规则（`.env`、密钥文件、
   `.git`等）；绝对路径、`..`、`~`、驱动器与提供程序路径、UNC一律询问。客户端再把路径解析符号链接，确认仍在工作区内
   且不落到敏感文件上。Windows上额外拒绝尾部的点和空格、8.3短名称、`-参数:值`写法和逗号数组。
5. **“以后不再询问”保存命令前缀。** 审批窗口的第二个按钮在命令可以保存规则时显示“批准并以后不再询问：<前缀> *”。
   前缀取命令名；带子命令的工具取到子命令（`git push *`、`npm test *`）；运行器取到实际程序（`python -m unittest *`、
   `uv run pytest *`）。以后以该前缀开头的简单命令自动批准，原因写“匹配你保存的规则”。
6. **不提供保存的命令。** 删除（`rm`、`Remove-Item`等）、提权（`sudo`等）、Shell与能执行内联代码的写法
   （`bash -c`、`powershell -Command`、`python -c`、`Invoke-Expression`等）、带路径的脚本。这些只能逐次批准，或在本会话
   内允许完全相同的调用（原有行为）。
7. **规则按工作区保存在本机。** 位置是该工作区状态目录下的`approval-rules.json`（私有文件），不进入代码仓库，随
   `harnessix data delete`一起删除。`harnessix code approvals`列出规则，`--remove 序号`撤销。规则按Shell种类保存。
8. **三个平台同一套规则。** Windows上对所有命令生效；macOS、Linux只对沙箱外命令生效，沙箱内命令本来就自动执行。

## 与Claude Code的差异

- `grep`、`find`、`Select-String`不在只读集合内：递归搜索能读出敏感文件内容，`find`带执行与删除参数。Agent有自带的
  搜索工具，不需要审批且遵守敏感名称规则。
- 只读命令沿用读工具的敏感名称规则并复核符号链接；Claude Code只限制在工作目录内。
- 没有允许/拒绝规则配置文件，不解析PowerShell语法树拆分复合命令，不做别名归一之外的规范化。后续需要时另行决定。

## 结果

### 正向结果

- Windows上查看类命令不再打断用户；重复运行的测试命令批准一次即可。
- 审批只留给真正需要判断的操作，减少盲目批准。
- 每次自动批准都有审批记录和原因，可审计；规则可见、可撤销。

### 成本与风险

- 判断基于命令文本。规则宁可多询问：新出现的绕过写法应当先加测试再修。
- `git diff`、`git log`会执行仓库配置里的外部diff、textconv等程序。规则假定工作区的`.git/config`可信：Agent的写工具
  不能写`.git`，写入只能通过用户批准的命令发生。
- 保存的前缀之后的参数不受限制，例如保存`git push *`后`git push --force`也会自动执行。保存是用户的显式选择，按钮上
  显示完整前缀。
- 文本判断与符号链接复核之间、复核与执行之间存在时间差；本机同用户的并发篡改不在防护范围内，与读工具的边界一致。

## 被否决方案

- **服务端自动放行只读命令**：会改变无人值守运行和评测的行为，并让“每条沙箱外命令都经过用户”的不变量失效。
- **只做“本会话允许相同操作”**：重启后失效，参数稍有不同就不匹配，解决不了审批疲劳。
- **Windows操作系统级沙箱（MXC、受限令牌、WSL2）**：用户决定不做。
