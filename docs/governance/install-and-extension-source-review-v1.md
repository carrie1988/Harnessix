---
doc_type: governance
status: current
version: 1
code_revision: 7a39f28bfb0d82e75a9e7a8b677c642d7733f845
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
supersedes: []
---

# 安装脚本与扩展来源审查 v1（0.9.4b）

## 1. 审查范围与结论

本次审查覆盖全部安装入口（源码安装、Makefile、uv锁定、Dockerfile、CI工作流）与全部扩展来源（MCP Target、Skill Root、Hook注册、内置Task Pack）。结论：安装链输入全部可锁定、可校验；CI动作全部按完整SHA固定；扩展来源均受目录摘要/白名单约束，默认产品不自动装配任何远端或第三方可执行内容。审查同时发现并关闭两类缺口：`actions/checkout`此前按版本标签而非SHA固定（已全部固定为`1af3b93b6815bc44a9784bd300feb67ff0d1eeb3`）；Anthropic适配器直接使用的`httpx2`与`httpx2-jsfetch`未在[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)登记（已补充）。

## 2. 安装入口

| 入口 | 约束与复核结论 |
|---|---|
| `make install` / `uv sync --locked --all-extras --dev` | 全部依赖由`uv.lock`逐包SHA-256固定；CI使用`--locked`禁止解析漂移。 |
| [Dockerfile](../../Dockerfile) | 基础镜像`python:3.12-slim`，非root（uid 10001），仅安装`.[observability]`；无构建期网络脚本。 |
| CI工作流（13个） | 全部第三方动作按完整40位SHA固定，由[`test_workflow_actions_are_sha_pinned`](../../tests/governance/test_supply_chain.py)持续门禁；`setup-uv`按SHA固定并锁定Python 3.12。 |
| 发布Wheel | `pyproject.toml`声明依赖范围；sdist排除`benchmarks/taskpacks/*/solutions`，不携带Golden Patch。 |

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
- 许可证/SBOM/Secret扫描见[许可证权利链审查](license-rights-chain-v1.md)第4节。
- 新增扩展来源（如0.9.4d远端MCP）必须先完成目标身份、凭据生命周期与受管出口设计，再进入目录。

## 5. 边界

本审查不评估上游动作与镜像自身的内容安全性（以SHA与Digest固定版本并复用上游安全更新流程）；不覆盖用户自行安装的第三方MCP Server/Skill内容；不替代1.0前的完整渗透测试。
