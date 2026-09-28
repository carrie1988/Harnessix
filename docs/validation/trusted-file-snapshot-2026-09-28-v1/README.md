---
doc_type: validation-evidence
status: current
version: 1
code_revision: 8340ff1cbc6375ad4064b8be6bd4c7bd708c559d
owners: [core]
modules: [tools, product_config, delivery]
related_adrs:
  - docs/adr/0023-workspace-read-tools.md
  - docs/adr/0027-prepared-patch-and-write-admission.md
  - docs/adr/0104-managed-session-key-and-default-root.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_file_snapshot.py
  - tests/tools/test_runtime.py
  - tests/tools/test_windows_read_adapter.py
  - tests/tools/test_paging_feedback.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_config/test_session_key.py
  - tests/product_config/test_managed_session_root.py
supersedes: []
---

# 可信文件快照与POSIX密钥目录观察验证报告

## 1. 摘要、范围与固定源码

默认模型读取现在提供完整原始文件字节摘要，受管修改可以使用真实读取结果作为前置条件，
而不是把分页revision或可见文本片段当作完整内容SHA。此项修复确定的产品接口缺口，
不是新完整编码质量成绩，也不关闭1.0商用发布门禁。

| 源码 | 变化 | 验证边界 |
|---|---|---|
| `812ae7cfa1978acd53a278637b19f936ebac4a14` | 新快照合同、两个读取端口、默认Tool目录与编码指令v2 | macOS实际FD及默认产品闭环；Windows替身/原生测试入口区分 |
| `8340ff1cbc6375ad4064b8be6bd4c7bd708c559d` | POSIX目录安全身份与读取后权限/ACL复核 | 原独立回归FAIL保留；新增确定性复现和后继固定源码回归 |

总体架构、时序、数据流、接口、字段、伪代码及失败语义见
[快照完整详设](../../changes/m09-r3-trusted-file-snapshot.md)；目录观察小修复同步
[托管密钥原完整设计](../../changes/m09-4a-managed-session-key-and-root.md#411-目录安全身份与普通文件完整观察)，
不新增另一套Key Backend或恢复体系。

## 2. 合同、资源与安全事实

- 新公开合同为`harnessix.read-file-snapshot/v1`，字段包括`file_bytes`、`digest_status`和`content_sha256`。
- 默认`read_file`采用Tool major 2；其他只读目录major保持1。旧目录指纹不能重绑定新成功结果。
- 旧库级`ReadFileOutput`及两个平台的旧`read_file`不变；254份既有公开Schema与整改前逐字节一致。
- 完整摘要扫描上限2 MiB，原始字节保留BOM、CRLF和末行语义；可见文本仍受24 KiB及4 KiB行上限约束。
- POSIX摘要与分页各有扫描上限，共享5秒协作Deadline；不是总共只扫描2 MiB或可硬抢占阻塞I/O。
- POSIX大文件返回有界页、`omitted_limit`及空摘要；Windows超过原完整观察上限仍固定拒绝。
- 读取后和审批后文件漂移均不得覆盖实际当前内容。审批后漂移沿用既有`interrupted/uncertain_effect`保守语义，
  未Claim、未写入、不继续调用模型、不自动重放；不把这一状态计为任务成功。
- 新结果走已有Session、模型视图及审批/事务事实，不增加数据库迁移或更改Task Pack和评分阈值。
- Root目录条目及时间变化不是目录对象替换；目录仍要求owner、700、无Darwin扩展ACL及原dev/ino路径身份。
  普通Key/锁文件的完整状态、600、单链接及ACL检查不减少。

[合同事实](contract-facts.json)保存精确Schema摘要、实际常量及一个自有临时文件的真实快照：
可见页被截断，但完整SHA覆盖两行原始字节，且不同于分页revision和可见页摘要。
这份低敏Fixture不来自用户代码或任何Key文件。

## 3. 真实执行结果及不同环境

| 验证 | 固定源码/环境 | 结果 |
|---|---|---|
| 默认摘要红测试 | 整改前生产源码；macOS ARM64 | 6失败、1通过；缺少新输出字段 |
| 最终快照专项 | `812ae7c`；Python 3.13.8 | 32通过，2.11秒；包含最终BOM用例 |
| Tools/Context/Product/Delivery/Evals相关回归 | `812ae7c`实现；Python 3.13.8 | 974通过、13跳过，141.72秒 |
| Agent/Models/Trusted Actions/App Server/Protocol | `812ae7c`；Python 3.13.8 | 1861通过，65.20秒 |
| 原独立相关回归 | `812ae7c`；独立工作树、Python 3.12.7 | **1失败、974通过、13跳过，158.82秒** |
| 目录观察确定性红测试 | `812ae7c`生产源码；Python 3.13.8 | 5失败，0.08秒 |
| 密钥及默认Root最终专项 | `8340ff1`；Python 3.13.8 | 46通过，1.55秒 |
| 后继独立相关回归 | `8340ff1`；独立工作树、Python 3.12.7 | **984通过、13跳过，153.67秒** |
| 完整源码类型检查 | `8340ff1` | Mypy 364个源码文件通过 |
| 静态与合同 | 最终工作树 | Ruff、Schema一致性、可读性和文档治理检查通过，详见验证文件 |
| 图示 | 3幅快照新图及1幅修改的Key流程图 | 实际渲染并逐图视觉检查；源及PNG在本目录 |

相关回归组互有重叠，不把这些计数相加作为全仓通过数量。首次Python 3.13相关回归在收集完成后
才补最终BOM测试，因此该例由32项专项及后继独立回归覆盖，不计入974项旧批次。
本批次没有执行全仓回归，也没有执行Windows原生或Linux实际宿主验收。

默认产品测试使用真实Workspace、Tool Runtime、Session、审查Artifact及SDK审批，
Provider仅为离线脚本。摘要只能从前一轮真实Tool Result取得，不预埋期望SHA答案。
OpenAI和Anthropic测试使用实际Adapter与Mock Transport证明线协议字段可达，均不证明线上模型编码质量。

## 4. 原失败保留与目录观察归因边界

原Python 3.12批次中，旧未证明历史应在构造Provider前返回`publication_history_unproven`，
但先返回`publication_key_unavailable`。同源码孤立用例通过不能覆盖原FAIL。

新增确定性注入在Root首次fstat与路径lstat之间创建普通文件、目录或修改时间，三项均复现旧目录
完整元组比较的误拒绝；另外两项证明读取Key后放宽Root/私有目录权限原先未被拒绝。
修复只分离目录安全身份，并增加返回前安全复核，不重试、不补签、不生成另一Key。
新增两个真实目录替换及两个Darwin返回前ACL反例，确保新规则不会接受危险对象。

这证明一类目录观察缺口及相应修复，**不能证明原独立批次失败的排他根因**。
后继固定源码整组通过用于验证当前相关范围，不改写原失败结果或宣称所有偶发问题已消失。

## 5. 证据清单、Manifest与复验入口

| 文件 | 作用 |
|---|---|
| [contract-facts.json](contract-facts.json) | 合同、精确Schema摘要、原始字节Fixture、资源及Key安全不变量 |
| [verification.json](verification.json) | 原失败/红测试/后继结果、源码、环境、持续时间与未执行范围 |
| [review-packet.json](review-packet.json) | 评审边界、未证明事项和R1～R6开放工作 |
| [bundle-manifest.json](bundle-manifest.json) | 除Manifest自身外各件的精确字节数和SHA-256 |
| `artifacts/*-summary.txt` | 实际日志中的失败测试ID与最终摘要；原日志字节数/摘要另存验证文件 |
| `artifacts/*.mmd / *.png` | 三幅快照图和一幅Key流程图的源与实际渲染结果 |

日志摘录不保存测试局部变量、Key、数据库、模型正文或用户Workspace。
原完整日志保留在验证宿主，摘录过滤条件与原件摘要均登记，不把摘录当作完整原日志。

```bash
uv run pytest -o addopts='' -q tests/tools tests/context tests/product_config tests/delivery tests/evals
uv run pytest -o addopts='' -q tests/product_config/test_session_key.py tests/product_config/test_managed_session_root.py
uv run mypy src
uv run python scripts/generate_specs.py --check
uv run python scripts/readability_report.py --check --check-final-report --quiet
uv run python scripts/documentation_check.py
```

所有测试入口仅选择已跟踪的明确目录；不读取、执行、修改或打包未跟踪安全草稿。

## 6. 发布风险与下一步

本批次真实模型请求及新增Provider费用均为0；不重置现有预算，也不把之前鉴权Smoke重复计为新验证。
原冻结Container镜像尚未就绪，3仓10 Case/20 Trial尚无整改后完整真实运行；原[0/20结果](../provider-engineering-2026-09-20-v1/README.md)
保持不变。后续必须使用冻结镜像、任务清单、Grader、重复次数及阈值，不挑选成功Case替代完整Suite。

完整产品同机同用户备份恢复、12件许可复核、Windows原生写入与三平台发行、独立Beta、最终封板仍开放。
本报告不声明全仓、六实例CI、Windows产品支持或商用1.0就绪。
