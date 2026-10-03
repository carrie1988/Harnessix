---
doc_type: validation-evidence
status: current
version: 1
code_revision: daf33bf6e10c6d34ae7a9fe0d87cf74adbf234ee
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_first_failure_projection.py
  - tests/governance/test_windows_git_native_failure_projection.py
  - tests/governance/test_windows_git_trace2_input_binding.py
supersedes: []
---

# Windows 原首失败九字段发布接合验证

## 1. 结论与覆盖范围

原探针已生成、解码并保留的首失败九字段，现可沿现有发布链进入显式 v2 诊断 Sibling。
**九件相关离线测试文件实际 1034 通过，0 失败/错误/跳过，2.860 秒；462 件执行输入前后零漂移。**
其中包含新增文件 90 项和原八件 944 项，不与各初验集合相加。

该结果证明版本化发布接缝及原保护兼容，**不是 Windows 原生 Git 故障修复**。
没有新原生运行；Run37103867851 的 Git128/Worker2、RootUNKNOWN、proof ABSENT、SDKfalse 保持原事实。
完整总体/详细设计、四幅图、九字段解释、伪代码、关联及失败边界见
[设计第13节](../../changes/m09-r4-windows-trace2-role-input-binding.md#13-首失败九字段有限发布与固定输入接合)。

## 2. 原来源、版本与保护

- 数据源是同一 write 操作中、经过原 receipt/raw/EOF/SHA/protection 守卫后的原有限帧；不重读业务正文。
- 首失败直接复用原 Worker 九字段 validator，保留原 stage/code 白名单、实际类型、数值范围及一致性。
- v1 仍只含原三个有限字段；v2 只追加封闭九字段载荷，内层 Worker schema 不变。
- 缺失、拒绝、`False`、`None` 与首失败/最终 handler 差异不改写，原 status/返回码关联不符则失败关闭。
- 冻结 v1 读取不补键或升级；新报告中的混合合法旧行只表达新字段 `NOT_AVAILABLE/None`。
- assurance 始终为 `UNAUTHENTICATED_DIAGNOSTIC_ONLY`；原案例、branch、proof、SDK、Root、13 hooks 与期限不变。

## 3. 精确发行输入与原失败

原 18 件输入保留，前 17 件完整字节不变；metadata 仅最后一行的长度/SHA、精确 CRLF 长度/SHA 四叶改变，
parser 只替换唯一 metadata SHA 字面量。原基线、PE/PDB、guards、selectors、预算与历史结果不改。

实现初验保留一项真实旧批准锚点 FAIL：新合同字节不再匹配旧完整摘要。
精确差分和实现审查后，原整体 guard 只接合两件合同的新完整长度/SHA；
旧测试与固定基线比较，除这三个字面量外逐字一致，全部断言、五件历史字节及其他两件批准身份保留。
不是关闭、跳过 guard，也不是按语义相同自动接受新输入。该初验失败不重写为通过。

## 4. 实际执行与证据

运行基准为主仓 `daf33bf` 及本次明确变更的实际文件，使用 Python 3.12.7、本机 macOS，pytest umask022；
没有声明同步执行了 Windows Job。`verification.json` 的 selection 列出九件原相关测试文件。
执行输入覆盖全部生产 Python 和本组相关观察器、测试及配置，不宣称全仓所有文件都已执行测试。

[verification.json](verification.json) 给出数量、输入锁、源码 SHA、原件 SHA、版本及未证明项。
私有原报告保留初验 FAIL、命令、各阶段源码及结果；独立整合目录保存实现审查、旧测试精确替换核验、
最终 XML/日志/退出码、执行前后输入锁、四图及交付清单。目录 0700、文件 0600。
公开资料不包含实际 raw/stderr/CDB、路径、PID、凭据或业务正文。

## 5. 仍未证明的范围

本次没有取得新的 Windows 现场首失败值，不能选择实际 stage/origin/error_code、Git 内部 caller 或 errno 为根因。
尚未证明独立原生分支、实际输入读取、对象写入、SDK 成功或消费者平台验收。
完整 Git/Backup v2、R3 编码质量、独立 Beta 及 R1～R6 发布门禁仍开放。
未读取凭据、发送模型请求、修改费用、重跑 CI 或覆盖旧验证资料。
