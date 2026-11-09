---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [skills, trusted_actions, product_config, product_ui, agent]
related_adrs:
  - docs/adr/0074-skill-snapshot-and-hook-action-boundary.md
  - docs/adr/0076-code-readability-and-structural-governance.md
related_tests:
  - tests/skills/test_agent_tools.py
supersedes: []
---

# ADR 0125 Skill的产品装配

- 状态：评审中
- 日期：2026-11-09

## 背景

ADR 0074实现了Skill的发现、快照和只读Action，但默认产品没有接入，用户装了也用不上。接入时遇到四个具体问题：

1. 现有Action的入参要求模型提供目录摘要和清单摘要，共两个64位十六进制串，模型无法可靠抄写；
2. 现有Binding的指纹按目录内容计算，与Agent Gateway要求的工具描述指纹不一致，无法注册给模型；
3. 目录内容一变，工具合同就变，崩溃后恢复时未完成的调用会被判为合同变化；
4. 状态备份只接受固定文件清单，状态目录里多一个`skills.db`，所有装了Skill的用户备份都会失败。

## 决策

1. **来源**：只读两个位置，`~/.harnessix/skills`和`<工作区>/.harnessix/skills`。目录不存在时不创建，也不装配任何东西。这两处都在受保护的`.harnessix`之下，Agent的文件修改和沙箱命令不能写入。
2. **模型入口**：新增`build_skill_agent_tools`，工具仍叫`skill.load`和`skill.read_resource`。模型只提供名称和资源路径，宿主按本次启动冻结的目录补齐摘要。名称、路径错误或内容被改动都是普通工具失败，模型可以换名称重试，不中断Turn。
3. **工具合同与目录内容无关**：版本固定为`1`，描述不含目录信息，编辑Skill后未完成的Turn仍能恢复。目录身份在规划时写入Action资源，成功输出按该资源校验（`builtin_success._skill_content_is_planned`）。
4. **统一入口**：`CompositeAgentActionGateway`按工具名分派，内置Action和Skill仍走同一个Agent入口，路由、审计和输出校验不变。
5. **目录快照只在进程内**：写进私有临时目录，每次启动重新发现，不进入状态备份。每次调用的审计已由Action Audit持久化，Skill Store自身的访问事件在产品中不再持久。
6. **模型可见内容**：名称和描述按项目指令优先级进入Context，整体不超过48项、8 KiB，超出时说明省略数量。正文只经工具读取。这段内容是不受信数据，不能提升权限。
7. **目录损坏不阻塞启动**：发现失败时记录日志并按无Skill运行，`harnessix skills list`显示同一错误和具体文件。
8. **只在通用执行下宣告**：固定流程的工作流视图不暴露Skill工具，此时不写入清单，避免提示模型调用不存在的工具。
9. **依赖方向**：`product_config`作为组合根依赖`skills`。`trusted_actions`原本为判断具体类型而延迟导入`product_config`，这条向上依赖会把`skills`卷入依赖环。现改为下层定义标记基类`OfficialWorkspacePatchReview`，由产品的Review继承，`product_config`因此退出原依赖环。

## 运行时修正

`AgentRuntime._parallel_read_prefix`此前只看工具描述，只读、低风险且允许并行的Trusted Action会绕过`prepare`，报`approval_mismatch`。此前没有这样的工具，缺陷未暴露。现在Trusted Action一律走串行的`prepare`路径。

## 被拒绝的方案

- **让模型传摘要**：实现最少，但模型抄错一个字符就失败，尤其是本地模型。
- **把Skill并入内置Action目录**：内置目录的能力证据绑定产品配置，Skill是运行时发现的内容，放进去会让配置摘要随文件变化。
- **`skills.db`放进状态目录并扩展备份清单**：备份清单是冻结合同，且目录可以重新生成，不值得改合同。
- **默认读取`.claude/skills`、`.agents/skills`**：会把其他产品的内容隐式带入，是否兼容待产品决定。

## 结果

- 没有Skill时，工具列表和提示词都不变，评测口径不受影响；
- 一次启动内目录固定，运行中修改Skill要重启才生效，修改后读取旧Skill会明确失败；
- 崩溃恰好发生在读取Skill的瞬间时，该调用以需要人工介入的状态结束，不会自动重放；
- 只支持只读文本内容包，不执行脚本。

## 预算说明（ADR 0076）

本次增长的预算，原因见[门禁恢复设计](../changes/p0-ci-gate-recovery.md)第7节。

## 验证

- `tests/skills/test_agent_tools.py`：完整AgentRuntime链路、名称错误、路径穿越、同名冲突、内容篡改、中断恢复、清单上限、无Skill时零影响；
- 用已安装的wheel接本地模型实测：提示词未提及Skill，模型自行读取Skill及其资源文件，并按其中规则修改代码。
