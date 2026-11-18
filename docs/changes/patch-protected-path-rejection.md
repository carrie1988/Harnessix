---
doc_type: change-design
status: current
version: 2
code_revision: 9c6c60ac3954edbe8943493b8b1c07d313c807aa
owners:
  - core
modules:
  - trusted_actions
related_adrs:
  - docs/adr/0137-create-with-missing-parent-directories.md
  - docs/adr/0172-key-and-certificate-files-are-writable-with-hidden-diffs.md
related_tests:
  - tests/product_config/test_patch_protected_paths.py
supersedes: []
---

# 规划时写受保护路径只拒绝这次调用

> 第2版（2026-11-18，ADR 0172）：密钥与证书文件（`.key`、`.pem`、`.p12`、`.pfx`）在所有平台上都可以由补丁写入，
> 内容从不显示，见[补丁可以写密钥与证书文件](key-certificate-patch-writes.md)。受保护的只剩凭据目录与文件名
> （`.git`、`.harnessix`、`.codex`、`.ssh`、`.aws`、`.gnupg`、`.env*`、`id_rsa`等），两个平台规则相同。
> 本文其余部分描述第1版的修复：受保护路径只拒绝这次调用，这一点不变；写给模型的说明不再列出密钥与证书文件。

## 1. 文档摘要

模型用`apply_patch_batch`新建受保护的文件时，整轮任务此前会被中断。受保护的文件指密钥、证书或凭据类文件，
例如`.key`、`.pem`、`.p12`、`.pfx`，以及`.ssh`、`.env`下的文件。现在这次调用以`delivery_path_denied`失败，整批不写入，
模型收到原因说明，任务继续。只涉及macOS与Linux：Windows规划不按文件名保护这类路径，补丁照常写入，本次不改变（见第11节）。

## 2. 需求背景

2026-11-17 NL2RepoBench冒烟（wsgidav）：模型在一批里新建`wsgidav/server/sample_bogo_server.key`等示例证书文件，
任务在第30步以`uncertain_effect`（触发原因`runtime_error`）中断，得分为0。0.10.0b48上同样复现。
Web服务类项目常有示例证书与测试密钥，真实用户同样会遇到。

## 3. 设计目标与非目标

- 目标：在macOS与Linux上，受保护路径只拒绝这一次调用，并给出可操作的说明。
- 非目标：放开受保护路径的写入；改变命令工具的行为；改变Windows上的行为（密钥与证书文件是否一律禁止补丁写入，由负责人另行决定）。

## 4. 总体架构

可信操作规划（`trusted_actions/planning.py`）先观察要写的路径。POSIX的观察根（`workspace/snapshot.py`的`_PosixRoot`）复用读取工具的Workspace，
因此附带执行了读取策略（ADR 0137称之为附带行为）；Windows的`WindowsWorkspaceRoot`只规范路径语法。策略拒绝的路径以
`ReadToolError("path_denied")`失败，而规划没有转换这一类错误（其余步骤都经`sanitize_plan_exception`），
异常一直冒到运行循环。网关`prepare_action`现在把规划失败一律交给`workspace_patch_planning_rejection`：
对内置Patch，`path_denied`的读取错误折算为Delivery层已有的`delivery_path_denied`，与原有的规划拒绝一样
投影为可修正的失败结果；其余情况返回None，原异常照常抛出。规划代码不变。其他可信操作沿原错误处理：
命令只声明根目录，Git检查点与回滚只用已观察过的路径，实际都走不到这里；即使走到，也不会给出“Patch”措辞的拒绝。

## 5. 接口设计与数据结构

不新增错误码与合同；写给模型的英文说明补上密钥与证书文件。

## 6. 流程与数据流

模型提出调用 → 规划观察Workspace → 路径受保护，报`delivery_path_denied` → 网关生成失败结果
（整批未应用） → 模型继续。

## 7. 失败、恢复、取消与超时

此前：未知异常 → `runtime_error` → 写操作没有结果 → `uncertain_effect`，整轮中断。
现在：确定的失败结果，没有未知效果。其余读取错误（超时、过大等）的处理不变。

## 8. 持久化与数据流程

规划尚未保存任何Route，没有需要关闭的计划；会话里只记录失败的Tool Result。

## 9. 安全与信任边界

macOS与Linux上受保护路径照旧不能由补丁写入，改变的只是失败的报告方式。Windows上补丁照旧可以写入这类文件（0.10.0b48起即如此），本次不改变。

## 10. 可观测与错误分类

错误码`delivery_path_denied`；界面写“Workspace Patch路径不允许写入”。

## 11. 部署、兼容与回退

撤销提交即可。网关的转换三个平台相同，但只有POSIX规划会报出`path_denied`，Windows上走不到这条转换。

## 12. 限制与风险

模型仍可以用命令工具写这类文件。产品的命令路径不按工作区读取策略检查文件名，本次不改变这一点。

## 13. 验证

- `tests/product_config/test_patch_protected_paths.py`覆盖两代快照。第1版是密钥、同批混合、.p12、.ssh目录四种情况，只在POSIX运行；
  第2版改为`.ssh`目录、同批混合`.env`、`.env.local`、`id_rsa`、`.aws`目录、证书与`.gnupg`同批六种情况，不再限于POSIX，
  并断言说明不再列出密钥与证书文件。只对内置Patch转换的单元测试在任何宿主运行。
- 全量回归与b48对比。

## 14. 源码与测试映射

| 源码 | 测试 |
|---|---|
| `src/harnessix/trusted_actions/agent_gateway_support.py`（`prepare_action`） | `tests/product_config/test_patch_protected_paths.py` |
| `src/harnessix/trusted_actions/preparation_rejection.py`（`workspace_patch_planning_rejection`、`_planning_failure_code`与说明文字） | 同上，含`test_only_the_built_in_patch_turns_a_protected_path_into_a_rejection` |
