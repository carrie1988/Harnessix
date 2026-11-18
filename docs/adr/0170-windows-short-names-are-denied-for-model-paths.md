---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [workspace, tools, delivery, trusted_actions]
related_adrs:
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0128-host-command-approval-rules.md
  - docs/adr/0137-create-with-missing-parent-directories.md
related_tests:
  - tests/delivery/test_windows_short_names.py
  - tests/tools/test_windows_read_adapter.py
supersedes: []
---

# ADR 0170 Windows上模型提交的读写路径拒绝8.3短名称

- 状态：评审中
- 日期：2026-11-17
- 修订：Windows读取端口的路径入口、Windows Patch与Workspace事务的保护路径判断

## 背景

Windows（NTFS默认开启）为长名称生成8.3短名称：`.env`的短名称是`ENV~1`，`.git`是`GIT~1`，`.ssh`是`SSH~1`。
按短名称打开文件，系统落到对应的长名称上。

读取工具的敏感名称拒绝（`.env`、`.ssh`、`.aws`、私钥名、`.pem/.key/.p12/.pfx`）与写入的保护组件
（`.git`、`.harnessix`、`.codex`、`.env*`等）都按模型提交的字符串判断，不比较打开后的真实名称。
命令审批规则早已在Windows上拒绝`~`（ADR 0128），读取工具与Patch没有对应规则。

2026-11-17在Windows 11真机（D:盘8.3名称已开启）上用已发布的0.10.0b48复现，假密钥与无害内容：

- **读取：** `read_file(".env")`以`tool_path_denied`拒绝；`read_file("ENV~1")`把`.env`的完整内容返回给模型。
- **写入：** Patch新建`.git/hx-probe-83a.txt`以`delivery_path_denied`拒绝；新建`GIT~1/hx-probe-83b.txt`
  在自动接受修改下成功，文件写进了`.git\`。

于是模型（或仓库内容里的提示注入）可以读出`.env`、`.ssh`、`.aws`下的凭据并发给模型服务商，
也可以往`.git`（例如`hooks/`）或`.harnessix`写文件。macOS与Linux没有8.3名称，不受影响。

## 决策

1. **形状判断。** `workspace.paths.has_windows_short_name`：任一路径段含“`~`后跟数字”即视为8.3短名称。
   系统生成的短名称都是这个形状（`ABCDEF~1.TXT`、哈希形式`AB12F~1`）；`notes~`、`a~b.txt`不受影响。
2. **读取入口。** Windows读取端口`WindowsReadPort.path`在敏感名称策略之后再拒绝短名称，
   `read_file`、`list_files`、`glob`、`grep`的路径参数都经过这里，错误与敏感名称相同（`path_denied`）。
   目录遍历得到的都是长名称，逐条目过滤不加这条规则，不会隐藏真名含`~数字`的文件。
3. **写入入口。** Windows上Patch（`_normalized_files`）与Workspace事务规划（`_normalized_targets`）把短名称
   当作保护路径，以`delivery_path_denied`拒绝；给模型的说明加上“Windows上不要用GIT~1这类8.3短名称，改用完整名称”。
   这是规划期的可修正拒绝，本轮继续。
4. **只在Windows。** POSIX上`a~1.txt`是普通文件名，照常读写。通用的`normalize_workspace_path`不改：
   Git清单、树投影等也调用它，仓库里真名含`~数字`的文件不能因此让Git交付失败。

## 后果

- Windows上模型无法再用短名称读到敏感文件，或写进`.git`、`.harnessix`等保护目录。
- **已知局限：** 仓库里真名就含“`~`数字”的文件，在Windows上不能经读取工具与Patch按这个名称访问，
  拒绝说明提示改用完整名称；需要时可用命令工具。与ADR 0128对命令的取舍相同。
- **未覆盖：**
  - `git_diff`的路径参数不经敏感名称策略，是另一条读取通道，单独评估。
  - Linux挂载的VFAT卷也能按短名称解析，产品不支持把工作区放在这类卷上，本ADR不处理。
- 只比较形状，不取打开后的真实名称。真实名称比较（`GetFinalPathNameByHandleW`）能覆盖所有别名，
  但要改动Windows句柄链并在真机上全面验证，留作后续加固。

## 验证

- **单元测试：** `tests/delivery/test_windows_short_names.py`覆盖形状判断，以及Windows Patch、事务规划拒绝、POSIX保留。
  `tests/tools/test_windows_read_adapter.py`覆盖`ENV~1`、`SSH~1/config`、`src/GIT~1/config`拒绝，含`~`的普通名称可读。
  三个短名称用例在0.10.0b49代码上失败。
- **Windows真机：** 候选包重跑上面的读写复现，期望读取与写入都被拒绝、本轮完成、`.git`里没有探测文件。
