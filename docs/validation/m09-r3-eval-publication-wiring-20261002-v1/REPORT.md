---
doc_type: validation-evidence
status: draft
version: 1
code_revision: pending
owners:
  - evals
  - security
modules:
  - evals
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/evals/test_task_pack_publication.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
supersedes: []
---

# Eval 认证宿主接线离线验证

## 1. 验证基线与范围

工作树标识：`r3-auth-wiring/Harnessix`；Git 基线 `dfba34e707ed845f3e9d844461e124015c22dca7`。修改尚未提交，最终源码身份以 `source-sha256.json` 为准，不把基线提交误作变更后版本。私有验证记录保留实际解释器与源码绝对路径，公开设计使用工作树标识。

设计：[完整 SDD](../../changes/m09-r3-eval-publication-wiring.md)。范围仅为三条 Trial 路径、Case 前缀、真实 Suite 与预算宿主的同源 Scope 转发。未修改 Key backend、Session 读取切缝、Schema、费用规则、Pack、Profile 或 Grader。

## 2. 实际命令与运行来源

`OWNED_TREE` 必须指向独立接线工作树，`VALIDATION_PYTHON` 必须指向指定集成环境的绝对 Python；不得使用其他工作树的 `harnessix` 安装。实际值与启动来源见 `pytest-final-seam.txt`、`facts.json`。

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$OWNED_TREE/src" \
"$VALIDATION_PYTHON" -B \
docs/validation/m09-r3-eval-publication-wiring-20261002-v1/run_offline_tests.py \
<新的自有私有临时目录> \
tests/evals/test_task_pack_publication.py \
tests/evals/test_task_pack_execution.py \
tests/evals/test_task_pack_evidence_stop.py \
tests/evals/test_provider_suite_execution.py \
tests/evals/test_provider_suite_cli.py
```

Runner 覆盖项目原 `pythonpath=["."]`，使用 importlib 模式并只登记 `tests` 的明确包目录，不把仓库根加入 `sys.path`；启动记录验证 `harnessix.__file__` 来自本树 `src`。夹具进程 umask 为 022；新 Run／Key／验证目录依产品契约使用 0700／0600。复跑必须使用新自有夹具目录，不用历史 Run。

## 3. 实际结果与失败闭环

| 记录 | 实际结果 | 处置 |
|---|---|---|
| pytest-v1 | 83 passed / 6 failed | 夹具改用绝对 Workspace、正式 `EXECUTING_TOOLS`，为固定 Profile 提供本地非执行 Engine 文件 |
| pytest-v2 | 86 passed / 3 failed | Artifact 夹具补正式用户输入与 UsageRecorded 事实，不运行模型 |
| pytest-v3 | 95 passed / 2 failed | SQLite 夹具连接未关闭导致 SHM 变化；修正构造连接生命周期，不排除文件或改阈值 |
| pytest-v4 | 97 passed | 原场景通过 |
| pytest-v5 | 110 passed / 2 failed | Session 链接取得错误转稳定宿主错误；成本不完整用例保留真实 `cost_unknown` 优先级，不改费用计算 |
| pytest-v6 / v7 | 112 passed | 原 Key、MAC、Scope、Root、恢复与 CLI 回归通过 |
| pytest-final | 114 passed | 新增物化阶段 Owner 与关闭后拒绝借用 |
| pytest-final-seam | **114 passed / 1 xfailed** | 保留基线原事件正文 MAC 同读缺口，见第 5 节 |
| pytest-release | **114 passed / 1 xfailed** | 最终冻结源码重复复验，保留严格预期失败原因 |

Ruff、Mypy、文档结构／链接与图形渲染结果见独立记录。各失败日志保留，不删除失败制造全绿。

本包三个 Markdown 的元数据、结构、相对链接及四图检查通过。原变更同步门禁仍要求更新 `docs/modules/evals.md`；该现行模块文档不在此独立实现的编辑范围，由集成阶段同步。`documentation-check.json` 明确记录 `PENDING_INTEGRATION`，不将局部文档检查称为全库门禁通过。

## 4. 核心证据与边界

- 新临时 SQLite 原生事件、投影、Artifact 都具备认证证明；原 Key 身份及文件 SHA／修订跨重开保持。
- 合成旧夹具在 missing Key、missing Header、missing Event Seal 拒绝路径上全部文件 SHA 一致，Provider Factory 计数为 0；不存在 Key create/fallback、旧记录补签。
- 完成报告读取、终态 Session 实际重建报告、Case 前缀恢复共用传入 Scope，不触发 Provider；三条路径共享同一 Run Owner／Binding／Store。
- Root Owner 覆盖物化、创建、读取、执行和报告；Root inode 替换以及 Session 叶链接拒绝；作用域关闭后不可借用。
- 默认真实 Scope 非空且冻结材料；环境漂移不改变默认 Factory 材料。自定义真实 Factory 缺失／空 Scope 拒绝。
- 正常、异常、取消、重复取消、逻辑超时后原受托线程结算并清零迟到材料、释放锁。
- 预算入口仅 AST／源差异核对，不执行入口、账本或 Guard 请求；原规则和模型参数保持。
- 夹具为本次生成的合成数据；测试 Provider／Agent 只用于装配探针或录制事实，没有真实模型请求、网络或容器。没有读取实际旧 Run、Key、Keychain 或实际账本。
- 静止夹具 SHA 证明不等于活跃 WAL／SHM 字节不可变保证；没有将离线测试纳入真实 R3 成绩。

## 5. 剩余集成条件与风险

`test_completed_recovery_requires_original_event_body_mac` 标记严格预期失败：基线投影 Reader 可在原事件正文被篡改、投影及 Seal 保持时接受缓存投影。本包未修改 Session 源码或实现第二套 Reader。

Session 同事务原历史验真接缝集成后，必须移除 `xfail(strict=True)` 并在实际精确源码上复跑通过；不得将 xfailed 记为通过或发布认证闭环。MAC 来源认证与 Root 执行权分别归属 Session Reader 和产品 Run Owner，不能互相替代。

Windows 原生、真实 Provider 编码质量与真实费用入口均未执行。不能据本包启动新模型请求、释放未决预留或宣布 R3／商用发布完成。

## 6. 交付与审查

`source-sha256.json` 提供全部 Owned 源／测试／SDD 的实际 SHA 和基线 Blob／SHA；`scope-audit.json` 提供保护文件未改及预算 AST 等价证据；`facts.json` 提供有限事实；`REVIEW.md` 为集成清单；`manifest.json` 封存包成员；`verification.json` 为独立重算结果。

`owned.diff` 仅包含精确源／测试／SDD 增量；`verify_manifest.py` 可独立重算包成员，不修改既有报告。Manifest 不包含自身及独立验证结果，Manifest 自身 SHA 另行交付。验证脚本只有读取与标准输出，无模型、容器、凭据或账本入口。

本包不携带临时数据库、Key 文件、Scope 材料或事件正文。自有夹具和类型缓存完成验证后清理，失败记录、源摘要、设计和图形保留。

首次包卫生扫描使用无词界的凭据标记模式，误匹配任务包物化 Schema 文件名的后缀。独立记录 `package-seal-attempt-v1.json` 保留该失败；修正为凭据标记词界后重新扫描。文档门禁同样误匹配该文件名的直接引述，第二次失败保留于 `package-seal-attempt-v2.json`，正式报告改为语义描述，未修改原门禁规则。此处仅是有限模式检查，不宣称通用凭据检出保证，不改变源代码或测试结果。
