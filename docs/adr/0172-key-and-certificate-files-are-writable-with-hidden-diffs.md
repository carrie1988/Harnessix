---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [tools, workspace, delivery, trusted_actions, patches]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0137-create-with-missing-parent-directories.md
  - docs/adr/0170-windows-short-names-are-denied-for-model-paths.md
related_tests:
  - tests/product_config/test_patch_key_certificate_files.py
  - tests/product_config/test_patch_protected_paths.py
  - tests/delivery/test_key_certificate_patch_policy.py
supersedes: []
---

# ADR 0172 补丁可以写密钥与证书文件，其内容从不显示

- 状态：评审中
- 日期：2026-11-18
- 修订：补丁与回滚的写目标策略、两个平台共用的保护名称、Workspace事务Diff、编辑锚点拒绝说明、写给模型的拒绝说明

## 背景

0.10.0b49起，macOS与Linux上`apply_patch_batch`新建`.key`、`.pem`、`.p12`、`.pfx`文件以`delivery_path_denied`拒绝。
原因不是专门的写入规则：POSIX规划与发布复用读取工具的Workspace，读取策略按后缀拒绝这类文件（ADR 0137称之为附带行为）。
Windows规划只规范路径语法，补丁照常写入这类文件。两个平台的行为不一致。

Web服务类项目常有示例证书与测试密钥（2026-11-17 NL2RepoBench冒烟：wsgidav新建`sample_bogo_server.key`）；
拒绝之后模型只能改用命令工具，或放弃。另一方面，Windows上的保护名称只有`PROTECTED_COMPONENTS`与`.env*`，
`.ssh`、`.aws`、`.gnupg`与`id_rsa`等私钥文件名可以写入。

负责人于2026-11-18决定（选项B2）：密钥与证书文件在所有平台上都可以由补丁写入，但其内容从不显示给任何人；
凭据名称在两个平台上都受保护；读取工具不变。

## 决策

1. **写目标策略。** `tools.workspace.PatchTargetPolicy`继承`WorkspaceReadPolicy`，只把`denied_suffixes`置空：
   敏感名称（`.git`、`.harnessix`、`.codex`、`.ssh`、`.aws`、`.gnupg`、`.env`、私钥文件名）与`.env.*`照旧拒绝。
   `Workspace`新增`patch_targets`参数（默认False，即`WorkspaceReadPolicy`），默认Workspace的行为与作用域摘要不变。
2. **只由补丁与回滚选用。** 选择是显式的，不按`access == "write"`推断（父目录以`read`观察，`certs.pem/`这样的目录也必须可用）：
   - `TrustedActionDefinition.patch_targets`：内置补丁与回滚为True；Router规划时的快照捕获与执行前复核都按它传参。
   - 快照：`capture_workspace_snapshot`、`verify_workspace_snapshot`、`capture_snapshot_facts`、
     `capture_workspace_snapshot_v2`、`verify_workspace_snapshot_v2`、`verify_host_workspace_snapshot`增加`patch_targets`
     （默认False）；为True时工作区根经`_open_native(..., patch_targets=True)`以写目标策略打开。外部根与默认调用不变。
   - Workspace事务只服务补丁与回滚：两代规划器、大小写别名检查、事务端口的成员观察与发布、回滚后的空目录清理一律用写目标策略。
   - `planner._read_existing_file`增加`patch_targets`：补丁与事务规划传True；Git交付与Git检查点经`_read_existing`照旧用读取策略。
   - 技能、预检、编解码、进程与评测快照、Git交付来源仍用默认策略。
3. **两个平台的保护名称。** `delivery.planner._protected`在`PROTECTED_COMPONENTS`与`.env*`之外再拒绝读取工具的全部敏感名称
   （`.ssh`、`.aws`、`.gnupg`、`id_rsa`、`id_dsa`、`id_ecdsa`、`id_ed25519`等），POSIX与Windows相同；
   Windows仍拒绝8.3短名称（ADR 0170）。`trusted_action`改为复用这一个函数，不再各有一份。
   `PROTECTED_COMPONENTS`本身不改：计划校验与Git树投影在用，已保存的计划必须仍能加载。
4. **内容从不显示。** 末段以`.key`、`.pem`、`.p12`、`.pfx`结尾（不区分大小写）的路径（`is_key_or_certificate_file`）：
   - Workspace事务Diff（`build_workspace_diff`传`hide_key_files=True`）的文件段只写一行
     `Content hidden (key or certificate file): before=<sha256|absent> after=<sha256|absent>`，前后版本都不写正文。
     审批Review Artifact（审批窗口与`read_artifact`分页读到的内容）、结果里的`diff_preview`、回滚的Review与结果都来自这份Diff。
   - Git树Diff不传这个参数，字节不变。
   - 事务Blob与备份里的明文前后镜像保留，用于发布、对账与回滚。
5. **编辑锚点不透露位置。** `apply_exact_edits(conceal_content=True)`：锚点缺失、不唯一与编辑重叠的拒绝只给通用说明，
   不含行号、处数、片段，也不说改动是否已在文件里；建议改用`replace`整体发送新内容。错误码与`edits[j]`前缀不变。
6. **写给模型的说明。** `delivery_path_denied`不再把密钥与证书文件列为受保护，也不再建议运行时生成示例密钥；
   保留`.git`、`.env`、`.ssh`、`id_rsa`示例与Windows 8.3短名称一句。

## 后果

- macOS、Linux与Windows上补丁都可以新建、替换、编辑、删除与回滚密钥与证书文件；审批时用户看到的是文件清单与前后摘要。
- Windows上补丁不能再写`.ssh`、`.aws`、`.gnupg`与私钥文件名（此前可以）。
- 读取工具照旧拒绝这类文件，模型无法读回其内容；提案修复（`call_repair`）经读取工具观察，读不到时不补`expected_sha256`与权限，
  模型应使用上次结果里的`content_sha256`。
- **残留信息：** 锚点是否存在仍由拒绝本身反映（这是编辑语义的一部分）；Diff条目的`binary`标记与文件大小照常记录；
  模型自己提交的工具参数（含它写入的内容）仍在会话历史中。
- **未覆盖：** Git检查点复核与Git交付来源仍用读取策略，事务里有密钥或证书文件时这两条路径会拒绝，留待单独决定。
- **兼容：** 合同、Schema、错误码与已保存记录不变。升级前在Windows上发布、含这类文件的事务，升级后重新生成的Diff文本不同，
  只影响跨版本时仍在进行中的结果投影。

## 验证

- **产品路径：** `tests/product_config/test_patch_key_certificate_files.py`，两代快照：新建（含`certs.pem/`目录）、替换、编辑、
  删除与回滚都在批准后写入，Review、`diff_preview`与回滚结果不含正文任何一行；`read_file`、`grep`、`glob`、`list_files`照旧看不到；
  锚点三类拒绝没有位置信息。
- **保护名称：** `tests/product_config/test_patch_protected_paths.py`（产品路径，`.ssh`、`.env`、`.env.local`、`id_rsa`、`.aws`、`.gnupg`）与
  `tests/delivery/test_key_certificate_patch_policy.py`（`_protected`、`_normalized_files`、`prepare_workspace_transaction`，
  `platform="windows"`在任何宿主运行）。
- `tests/delivery/test_windows_short_names.py`照常通过。
- **Windows真机：** 候选包上新建`certs/server.key`后审批窗口只显示摘要，新建`.ssh/config`被拒绝。
