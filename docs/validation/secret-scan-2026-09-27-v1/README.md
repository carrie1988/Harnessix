---
doc_type: validation-evidence
status: historical
version: 1
code_revision: ad2f8e226b674e4787ad0002f0d038a2a81ccfef
owners:
  - core
modules:
  - documentation
  - secrets
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_secret_scan.py
  - tests/governance/test_supply_chain.py
  - tests/governance/test_cli_console.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 有界Secret扫描整改验证报告

## 1. 摘要、身份与结论

| 项目 | 冻结事实 |
|---|---|
| 对应缺陷 | SEC-094-B2：整文件/二进制跳过、压缩成员漏扫及未完成静默放行。 |
| 代码 | `ad2f8e226b674e4787ad0002f0d038a2a81ccfef`；[完整详细设计](../../changes/m09-4b-bounded-secret-scan.md)。 |
| 环境 | macOS（Darwin）arm64、Python 3.13.8、uv 0.9.1；没有使用真实Provider。 |
| 扫描合同 | `harnessix.secret-scan/v2`、六条固定规则及固定预算。 |
| 本地结论 | 87项专项、195项有重叠回归及3996 passed/32 skipped完整已跟踪回归通过。 |
| 干净源码发行 | 精确提交git archive构建Wheel/sdist，2007个物理输入完整检查，固定规则零命中。 |
| CI结论 | 前序0601ede六作业已成功；本扫描候选在证据冻结时尚未启动CI，不能引用前序作为当前通过。 |
| 发布判定 | **候选，不是0.9.4、0.9或1.0正式发布PASS**。许可证来源、不可变安装输入、攻击套件和远端MCP等仍开放。 |

源码安装环境的Python版本来自实际运行解释器；三平台CI配置的Python 3.12/3.13不是本地验证环境的替代证据。
所有JSON是允许公开的版本、数量、哈希和固定状态投影，不保存Secret、正文、私人路径或原始stderr。

## 2. 需求、实现与源码路径

v1四项负例均失败：示例配置整文件豁免、NUL二进制跳过、Deflate Wheel不检查成员、缺失输入返回空列表。
当前[`scan_paths`](../../../scripts/secret_scan.py)完整覆盖才返回命中列表，读取或格式失败抛固定未完成错误。

[`contracts`](../../../scripts/secret_scan_contracts.py)定义共享上限；
[`archives`](../../../scripts/secret_scan_archives.py)在对象创建前检查中央目录实际记录、连续本地记录和TAR头，
支持有界ZIP、TAR、gz/bz2/xz，不落盘解包。ZIP数据必须完整解码、长度及CRC一致，不允许隐藏尾部或孤立压缩成员。
明确BOM规范化、三平台实际发行物门禁、UTF-8诊断、成员位置序号与路径哈希见专项详设。

## 3. 验证方法与版本边界

| 检查 | 数量/结果 | 范围和不能外推的结论 |
|---|---|---|
| v1缺陷复现 | 4 failed | 同一内存合成值，未保存内容；证明四项控制缺口，不表示真实凭据泄漏。 |
| v2专项 | 87 passed | 正常/坏包、元数据预检、隐藏尾部、BOM、预算、取消/超时、变更、CLI状态及输出。 |
| 治理与计划错误 | 195 passed，17.83秒 | 含上述87项，不能相加作为独立测试总数。 |
| 全部已跟踪回归 | 3996 passed、32 skipped，352.07秒 | `uv run pytest --ignore=tests/security -q -o addopts=''`；未跟踪攻击草稿不纳入验收，不能宣称原始工作树make check通过。 |
| 清单冻结补充回归 | 196 passed，18.14秒 | 包含新证据的原字节/源码输入及版本边界检查，和195组重叠；运行时源码未改变。 |
| 静态检查 | Ruff、Mypy328源码、可读性、合同、Task Pack、现有许可证基线及SBOM通过 | 旧许可证脚本通过不关闭SEC-094-B3。 |
| 实际图渲染 | 新详设及变更总体设计的6图通过 | 首次默认Puppeteer缺浏览器，后使用已安装Chrome；没有把环境错误当语法通过。 |
| 变异诊断 | 500次、两个正常格式基线、无未分类异常 | 65个变异仍是合法元数据或公开内容，不记为攻击拒绝；不替代编号化威胁模型验收。 |

该代码提交没有改变Agent Runtime、数据库Schema或审批/对账语义，全部既有回归仍保留。
扫描规则只覆盖固定模式，任何“零命中”都带输入范围和版本条件；不存在未知Secret的结论没有成立。

## 4. 干净源码与真实发行物

从精确提交`git archive`生成独立临时源码目录，初始化仅用于受控文件枚举的Git索引，
使用既有Python 3.13.8和本机构建缓存离线构建。该目录不包含未跟踪攻击草稿、原工作树dist、数据库或用户配置。

| 发行物 | 原字节大小 | 成员数量 | SHA-256 |
|---|---:|---:|---|
| `harnessix-0.1.0-py3-none-any.whl` | 925658 | 371 | `59611155f684d89605b364198bcdb6d37fc17d490b1dcd234afd7a162cb88944` |
| `harnessix-0.1.0.tar.gz` | 6256014 | 1985 | `ea23d26595d1bee2ea135dc7afa26b6479d061a5c320e497589bf34a653ac357` |

两个归档的`solutions/`成员数均为0。扫描的2007个物理输入还包括源码和构建目录`.gitignore`，
不是2007个发行件，也不是递归成员数。成员数由验证通过后的可信构建产物重新读取。

发行件原字节不提交本仓库，只冻结消费对象哈希与数量。可据精确提交重建和复验，但构建后端没有全部锁定，
因此不承诺跨环境逐字节相同。后续CI或新提交构建出的包必须按自身身份验证，不能沿用上述ad2f8e2哈希。
这些候选发行件不构成0.9.5全新安装、升级或Windows完整运行验收。

## 5. Manifest、结构化数据与Review Packet

| 文件 | 职责 |
|---|---|
| [verification.json](verification.json) | 环境、原失败复现、重叠测试组、完整回归边界、静态及变异诊断。 |
| [artifact-facts.json](artifact-facts.json) | 干净源码构建方式、实际发行物哈希/大小/成员数和扫描输入口径。 |
| [ci-observation.json](ci-observation.json) | 前序CI准确SHA和六终态；当前候选尚未启动，严格分版本。 |
| [review-packet.json](review-packet.json) | 候选判定、已关闭本地缺口、全部独立开放门禁和不成立的声明。 |
| [bundle-manifest.json](bundle-manifest.json) | 五份文件原字节哈希和大小、八项准确提交源码输入；清单自排除。 |

目录按[.gitattributes](../../../.gitattributes)保存原字节，Windows检出不得将冻结哈希输入改写为CRLF。
模拟Git `core.autocrlf=true`检出后五份冻结文件仍全部匹配Manifest；该测试不替代真实Windows门禁。
后续验证建立新证据，不重写前序[a5fd57e候选包](../security-governance-2026-09-27-v1/README.md)的历史失败结果。

## 6. 复验入口、恢复及风险

```bash
uv sync --locked --all-extras --dev
uv run pytest tests/governance/test_secret_scan.py tests/governance/test_cli_console.py
make supply-chain
uv run python scripts/documentation_check.py
```

失败输入必须修复后全范围重跑，不发布部分扫描结果。同步IO不能由检查点保证硬实时取消，CI构建和扫描各有
两分钟外部期限；预算计数不是严格RSS。未支持格式必须审查后增加正式合同及负例，不允许整文件豁免。

剩余门禁：当前扫描版本三平台CI终态、SEC-094-B3版本/来源许可证证据、构建/安装/镜像工具输入固定、
公开错误全边界审查、TM-01～TM-13攻击回归、远端MCP、0.9.5安装与Beta及0.9.6 Provider发布证据。
没有因本次局部通过删除或关闭任何剩余范围。
