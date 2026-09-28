---
doc_type: validation-evidence
status: current
version: 1
code_revision: 4ec6fffafb553b5e09852cb91bb126c311e0b134
owners: [core]
modules: [context, product_config, agent, evals, models]
related_adrs:
  - docs/adr/0054-context-planning-and-inspection.md
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_agent_context.py
  - tests/product_config/test_server_and_cli.py
  - tests/context/test_windows_sources.py
  - tests/context/test_sources.py
  - tests/context/test_compaction_runtime_recovery.py
supersedes: []
---

# 正式产品Context统一装配及Provider鉴权验证

## 1. 验证范围与固定身份

本目录统一保存R3共享Context装配的低敏验证资料、合同事实、Review Packet与Manifest。
详细背景、架构、流程、类/接口/字段、伪代码、安全和恢复设计见
[统一装配详细设计](../../changes/m09-r3-product-context-composition.md)。

| 工作项 | 固定源码及环境 | 可证明的范围 |
|---|---|---|
| Context装配整改 | `4ec6fffafb553b5e09852cb91bb126c311e0b134`；本地macOS ARM64、Python 3.13 | 默认产品与Task Pack共享指令、动态Source及既有持久Compaction |
| 独立验收工作树 | 同一固定源码；本地macOS ARM64、Python 3.12.7 | 另行记录回归结果；不等于三平台发行验收 |
| Provider鉴权 | `7568eee82f78cb936717068f121ff32a4058b3be`；北京、非Thinking、指定Coder快照 | 正式Adapter能解析鉴权、完成极小文本请求并取得完整Usage |
| 真实工程Suite | 尚未启动 | 不发布质量得分，不把鉴权或Fake Provider测试当作真实Coding成功 |

鉴权请求发生在Context整改之前，**不能证明新指令装配已经通过线上模型验证**。
历史工程Task Pack v2严格成功与必需检查仍为[0/20](../provider-engineering-2026-09-20-v1/README.md)，原证据不覆盖、不挑选或重写。

## 2. 源码求证与失败复现

在`d1e8d71`上，默认stdio产品和正式Task Pack的`AgentRuntime`均未装配Context或自动Compaction。
对真实默认产品组合根增加“首个模型请求必须含instructions”的断言后，该测试直接失败，实际值为`None`。
修复后同一入口具有版本化Runtime指令、项目指令及真实Tool目录，而不是仅单测孤立Factory。

另一处开放缺口已明确：读工具的分页`revision`不是Patch要求的完整内容SHA-256。当前模型可达读取
尚未提供后者；指令已明确禁止混用或臆造。读写前置摘要闭环补齐之前不能认为R3真实编码能力已验收。

## 3. 验证结果与限定解释

| 检查 | 结果 | 限定解释 |
|---|---|---|
| Context/Product Config/Eval受影响回归 | 666通过、7跳过；115.30秒 | 不是全仓回归，也不是线上Coding任务 |
| 固定源码独立工作树回归 | Python 3.12.7；666通过、7跳过；125.01秒 | 独立虚拟环境、macOS ARM64；不替代Linux或Windows验收 |
| 最终聚焦回归 | 30通过、1跳过；2.68秒 | 包含真实默认产品组合根、凭据出站拒绝和新Context合同 |
| 新合同用例 | 16通过、1跳过；0.89秒 | Windows替身端口合同通过；真实Windows Handle用例在macOS跳过 |
| 完整源码Mypy | 364个文件通过 | 不代表未执行平台的API行为通过 |
| Ruff / Schema | 通过；公共Schema未改 | 未新增Session迁移，旧事实不补签 |
| 新Mermaid图 | 3个实际渲染并视觉检查 | 全文档图库存量不等于全部重新渲染 |
| Provider极小请求 | 1次完成、无失败、无重试 | 没有执行Tool或Coding任务 |

重启压缩测试从多个已完成Turn建立真实持久历史，关闭后重新打开Session，触发自动摘要和活动窗口，
验证摘要无Tool、完整尝试用量、Context Inspection及活动窗口。原有Source竞态、取消、超时和Compaction
崩溃恢复用例同时回归。项目指令含活动测试凭据时，Provider未调用，Session不保存该正文。

## 4. 百炼鉴权与费用快照

[Provider低敏回执](provider-probe.json)只保存模型、地域、时间、计数、状态和费用估计；
API Key由macOS钥匙串在验证进程内取得，不写入命令行、仓库、Provider响应正文或公开报告。

| 参数 | 观测或固定值 |
|---|---|
| Provider / 请求模型 | `openai_chat` / `qwen3-coder-plus-2025-09-23` |
| 地域 / 模式 | `cn-beijing` / 非Thinking |
| 最大输出 / 最大尝试 | 128 Token / 1 |
| 观测输入 / 输出 | 13 / 1 Token |
| 原价费用估计 | CNY `0.000068` |
| 验证周期预算快照 | CNY 70；未知预留0；扣除本次估计后69.999932 |

本次按[阿里云北京地域精确快照价格](https://help.aliyun.com/zh/model-studio/model-pricing)的
不超过32K输入档计算，输入4元、输出16元/百万Token，不扣缓存、免费额度或活动折扣。
这些是原价上界估计，不是供应商已扣款或账户余额。预算是测试宿主的预留/核算记录，不是供应商账户硬停止线；
无法确认成本的请求必须保留预留，不按零计入。

## 5. 真实Suite前置环境与阻塞

冻结Task Pack v2的镜像为
`python@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757`。
本机Docker Engine可用，但该镜像不可用；两次标准拉取返回EOF，临时窄作用域公共Registry Token
避开鉴权请求后，平台Manifest读取仍返回EOF。主机直接访问Registry和Token端点分别得到401及200，
因此主机HTTPS路径与Docker daemon路径不能混为一项成功证据。未确认EOF的具体根因。

没有修改全局Docker配置、镜像摘要、Task Pack或评分器，没有在检查前置条件缺失时发起真实Suite模型请求。
处理顺序是补齐可信文件内容摘要、取得原冻结镜像、固定干净Revision与完整Suite计划，再进行真实质量验证。

## 6. 证据清单、可重复性与Review Packet

| 文件 | 用途 |
|---|---|
| [`contract-facts.json`](contract-facts.json) | 指令版本/摘要、宿主估算预算、三来源与Compaction配置、冻结Pack摘要 |
| [`verification.json`](verification.json) | 执行环境、回归计数、静态检查、图渲染及未执行项 |
| [`provider-probe.json`](provider-probe.json) | 与Context整改分开的极小鉴权请求、Usage和费用快照 |
| [`review-packet.json`](review-packet.json) | 评审范围、守护边界、开放工作包与后续R3任务 |
| [`bundle-manifest.json`](bundle-manifest.json) | 本目录文件的字节数与SHA-256；不自包含Manifest摘要 |

可重复的离线入口：

```bash
uv run pytest -o addopts='' -q tests/context tests/product_config tests/evals
uv run pytest -o addopts='' -q tests/product_config/test_agent_context.py tests/context/test_windows_sources.py tests/product_config/test_server_and_cli.py
uv run mypy src/harnessix
uv run python scripts/generate_specs.py --check
uv run python scripts/readability_report.py --check --check-final-report --quiet
```

本目录不包含私有配置、钥匙串值、Session数据库、Artifact、Workspace、完整Provider输出或Container输出。
这是R3子切片证据；R1～R6均未因本资料自动关闭，未声明1.0商用发布就绪。
