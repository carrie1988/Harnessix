---
doc_type: adr
status: reviewing
version: 2
code_revision: pending
owners: [core]
modules: [tools, workspace]
related_adrs:
  - docs/adr/0145-whole-repository-search-on-large-repositories.md
related_tests:
  - tests/tools/test_pinned_root.py
  - tests/tools/test_windows_directory_cache.py
  - tests/tools/test_workspace.py
supersedes: []
---

# ADR 0160 一次读取操作只在开头与结尾核对工作区根

- 状态：评审中
- 日期：2026-11-13（第2版同日：Windows一次读取操作内复用已核对的目录句柄）

## 背景

0.10.0b32/b33 Windows Beta复测（GitHub Issue #21）：在公开的Dify检出（16,108个文件）里从仓库根整仓grep，6/6在约5秒报
`tool_timeout`，没有任何匹配；缩小到子目录后0.016～0.172秒返回。真实任务中4个会话共28次内置搜索超时。

这不只发生在Windows：macOS上用16,000个`.ts`文件的合成仓库同样在5.00秒超时、0条结果。性能分析（cProfile）：一次整仓grep调用
25万次`open`，占5秒中的2.8秒。原因是`tools/workspace.Workspace.open`每打开一个文件，读取前后都从“/”逐段重新打开并核对根
（`_current_root`，单这一项累计1.9秒），再逐段打开工作区内的路径；Windows的`WindowsWorkspaceRoot._open_chain`同样每次从盘符开始。
逐段重新核对根是为了发现根目录在读取期间被替换（TOCTOU），但按文件重复，代价随“文件数×路径深度”增长。

## 决策

1. **POSIX：根的固定属于一次读取操作。** `pinned_root(workspace, operation)`在开头按路径打开并核对根，把描述符记在这次
   `ReadOperation.pinned_root`上；其间`Workspace.open`从它的副本出发，不再逐文件重走根路径；结束时再按路径核对一次。
   - 固定的描述符始终指向原来的根目录：其间根路径被替换，读取仍在原目录里，不会落到别处；结束时的核对报`workspace_changed`，
     这次结果作废——与原来“发现替换即失败”的结论相同，只是发现的时点从“下一个文件”变为“本次操作结束”。
   - 工作区内各路径段的no-follow打开、类型与身份核对、读后修订核对都不变。
   - 固定只记在这一次操作上，`Workspace`对象本身不保存状态：同一工作区上并发的读取操作互不影响。
   - 工具Runtime的POSIX读取（列目录、读文件、glob、grep）都在固定的根下执行；单个文件的读取仍是开头、结尾各核对一次，与原来相同。
2. **Windows：根绑定之后句柄链从根开始。** `_open_chain`在根已绑定时不再从盘符逐级打开根之上的目录；根本身照旧以不跟随
   Reparse Point的方式打开，并核对对象身份与最终路径。根之上的某一级被替换（例如换成Junction），根路径会解析到另一个对象，
   在这一步被发现。根目录的句柄在`WindowsWorkspaceRoot`存续期间一直打开。
3. **Windows：一次读取操作内，每个目录只打开并核对一次（第2版）。** 第1版之后，发布会话在Windows真机上复测：Dify检出
   （16,108个文件）整仓grep仍约9秒、超时（约0.57毫秒/文件），因为每个文件仍从根逐段打开各级父目录。现在
   `windows_read_port.directory_scope`为一次读取操作建立[`DirectoryHandles`](../../src/harnessix/workspace/windows_directory_cache.py)，
   记在`ReadOperation.windows_directories`上：
   - 每个目录第一次用到时以不跟随Reparse Point的方式打开，核对“不是Reparse Point、是目录、最终路径在根下”，句柄保留到操作结束；
     其下的文件只打开文件这一段，文件本身的核对（不跟随Reparse Point、单一硬链接、最终路径在根下、读前读后修订）不变。
   - 目录句柄与会话根相同，共享读写、不共享删除：用户的编辑器与构建工具仍可在这些目录里新建、改名文件；这些目录本身在操作
     期间不能被改名、删除或换成Junction，所以按路径打开其下的文件，解析到的仍是核对过的目录。
   - 根与第1项的POSIX固定根相同，第一次使用前和操作结束时按路径各核对一次身份与最终路径；根之上的某一级在操作期间被替换，
     结束时报`workspace_changed`，这次结果作废（读到的内容不返回给模型）。
   - 目录数达到上限（8192）、打开目录或文件时遇到意外的系统错误（包括不存在），这一次观察退回原来的完整句柄链。
   - 只用于真实的`WindowsWorkspaceRoot`；缓存只属于这一次操作，结束时全部关闭。

## 没有改变的

- 读取时限（5秒）、扫描上限、输出合同与工具描述。超时仍是`tool_timeout`、没有部分结果；“超时返回已找到的部分结果”需要改输出合同
  （`truncation_reason`是封闭的枚举），属于新需求，只记录。
- 整仓glob的扫描条目上限（Issue #21的对照里另有6/6达到上限）。

## 后果

- 同一台macOS上16,000个文件的整仓grep：`include="*.ts"`由5.00秒超时降到2.34秒完成，不带include由4.55秒降到2.43秒。
- 根目录在一次读取期间被替换时，这次读取不再中途失败，而是在结束时失败；读到的内容仍只来自原来的根目录。
- Windows每个文件少打开“盘符到根”的那几级目录（第1版），并且不再重复打开工作区内的各级父目录（第2版）：每个文件只打开
  文件本身一次。实际收益与正确性由发布会话在Windows真机上用Issue的组件脚本验证。
- 第2版的代价：一次读取操作期间（至多5秒），用到的目录不能被其他程序删除或改名；在其中新建、改名文件不受影响。此前这些目录
  在每个文件的读取期间同样被短暂持有，且不共享写入。

## 验证

- `tests/tools/test_pinned_root.py`：60个文件的整仓grep在固定的根下只按路径核对根2次（不固定时超过100次），结果相同；
  操作期间根路径被替换，读到的仍是原来的内容，结束时报`workspace_changed`；固定只属于一次操作；Windows句柄链的各级目录。
- 原有`tests/tools/test_workspace.py`（读取期间根、目录、文件被替换即失败）不变并通过。
- `tests/tools/test_windows_directory_cache.py`（第2版，替身文件系统，三个平台都运行）：每个目录在一次操作里只打开一次、
  目录句柄共享读写；父目录是Junction、文件或最终路径在根外时如实拒绝；缺失、根本身、达到上限时退回完整句柄链；根在开头与
  结尾各核对一次，被替换即`workspace_changed`；缓存只属于一次操作，正常结束、失败结束都全部关闭。真实句柄语义（Junction、
  硬链接、长路径、根目录内改名）由`tests/tools/test_windows_native_runtime.py`在Windows上验证。
