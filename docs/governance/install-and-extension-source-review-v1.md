---
doc_type: governance
status: current
version: 3
code_revision: 0601ede74c01039411c8be85e4297debe9dff478
owners:
  - core
modules:
  - documentation
  - mcp
  - skills
  - hooks
related_adrs:
  - docs/adr/0073-mcp-catalog-binding-and-sandbox.md
  - docs/adr/0074-skill-snapshot-and-hook-action-boundary.md
related_tests:
  - tests/governance/test_supply_chain.py
  - tests/governance/test_secret_scan.py
supersedes: []
---

# 安装脚本与扩展来源审查 v1（0.9.4b）

## 1. 审查范围与结论

本次审查登记源码安装、Makefile、uv锁定、Dockerfile、CI工作流及MCP/Skill/Hook/内置Task Pack来源。CI动作已按完整SHA固定；但Docker基础镜像仍用可变标签，镜像内安装未消费`uv.lock`，文档工具的传递依赖也未形成完整固定清单，因此不能认定安装链全部可复现。扩展按各自显式装配与摘要边界执行，默认产品不自动装配任意远端执行内容。`actions/checkout`已固定为`1af3b93b6815bc44a9784bd300feb67ff0d1eeb3`；Anthropic适配器使用的`httpx2`与`httpx2-jsfetch`已补充到[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)，不等同于其全部发行许可证义务已核验。

## 2. 安装入口

| 入口 | 约束与复核结论 |
|---|---|
| `make install` / `uv sync --locked --all-extras --dev` | `make install`与CI均使用`--locked`，依赖由`uv.lock`的发行Archive SHA-256约束；这不证明解释器或构建后端自身已固定。 |
| [Dockerfile](../../Dockerfile) | 基础镜像`python:3.12-slim`，非root（uid 10001），安装`.[observability]`；镜像标签和pip依赖解析未固定，是后续发行供应链门禁，不声明已关闭。 |
| CI工作流（13个） | 全部第三方动作按完整40位SHA固定，由[`test_workflow_actions_are_sha_pinned`](../../tests/governance/test_supply_chain.py)持续门禁；`setup-uv`按SHA固定并锁定Python 3.12。 |
| 发布Wheel/sdist | `pyproject.toml`声明依赖范围；sdist排除`benchmarks/taskpacks/*/solutions`，不携带Golden Patch；构建后端仍为未精确锁定的hatchling，离线缓存不等于版本来源固定。 |
| Secret发行门禁 | [有界扫描v2](../changes/m09-4b-bounded-secret-scan.md)要求三平台先`uv build --offline --out-dir dist/secret-gate`，再扫描显式目录；缺失/空目录、未支持格式、坏包及预算超限阻断。当前为候选验收，固定六规则不能证明所有Secret均不存在。 |

## 3. 扩展来源

| 来源 | 当前边界 |
|---|---|
| MCP Target | 仅`container_stdio`（固定镜像Digest、强Container）与`in_process`（宿主显式装配）；目录捕获不可变、调用前Schema漂移检查；`streamable_http`仅为合同枚举，未实现不广告。 |
| Skill Root | 仅Bundled/User/Workspace显式Root；目录与Frontmatter摘要冻结、按需复核；普通名称仅在全局唯一时可用；内容不可执行。 |
| Hook注册 | 只允许宿主预注册低风险只读Action；非Bundled定义需精确摘要授权；`before_action`失败关闭。 |
| 内置Task Pack | `harnessix-engineering/v1`、`/v2`的仓库、许可证、来源Revision与`PROVENANCE.md`固定；`generate_engineering_task_pack.py --check`保证逐字节不漂移。 |
| 模型Provider | 仅从`ProductConfigV2`严格配置装配；Secret只以引用持久化；`SafeFallbackProvider`只在零暴露失败时切换。 |

## 4. 持续门禁

- 工作流动作SHA固定由测试门禁持续执行；新增动作必须按完整SHA引用。
- 许可证/SBOM见[许可证权利链审查](license-rights-chain-v1.md)第4节；Secret门禁输入、预算和公开诊断合同见[有界扫描详细设计](../changes/m09-4b-bounded-secret-scan.md)。
- 新增扩展来源（如0.9.4d远端MCP）必须先完成目标身份、凭据生命周期与受管出口设计，再进入目录。

## 5. 边界

本审查不评估上游动作与镜像自身的内容安全性（以SHA与Digest固定版本并复用上游安全更新流程）；不覆盖用户自行安装的第三方MCP Server/Skill内容；不替代1.0前的完整渗透测试。
