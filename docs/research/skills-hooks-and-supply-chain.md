# Skills、Hooks与供应链边界源码研究

## 1. 研究范围与冻结基线

本文为Harnessix Code 0.8.5提供源码事实与设计依据，范围包括Skill发现、元数据、渐进加载、冲突、版本、Hook生命周期、匹配、超时、取消、持久恢复与供应链信任边界。

| 来源 | 提交 | 证据范围 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | Skill Root快照、显式选择、同名消歧、隐式访问识别、Hook事件、并发与超时 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | 多来源Skill发现、远端缓存、版本切换、Skill Tool与Plugin加载 |
| Claude Code逆向仓库 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | Skill Frontmatter、Hook配置快照、来源策略、异步Hook与生命周期行为佐证 |

Claude Code仓库不是官方源码，只用于行为交叉验证。研究只吸收机制、约束和失败语义，不复制参考实现代码。

## 2. Skill参考实现事实

### 2.1 Codex

Codex将Skill元数据与`SKILL.md`正文分开处理。加载层按有序Root生成快照，保存规范路径、Scope、解析错误和缓存；选择层优先按结构化路径选择，再按文本名称选择，只有名称在Skill和连接器命名空间中均唯一时才接受普通名称。显式选择保留发现顺序，并排除禁用路径。

Skill Frontmatter限制名称长度、要求描述，并将多行空白规范为单行。界面资源只能位于Skill自身`assets/`或明确的插件共享资源根，绝对路径和越界路径被忽略。隐式Skill识别仅接受可静态解析的文档读取或脚本运行形态，复杂Shell表达式不被推断为可信访问。

**可吸收结论**：目录元数据与正文按需加载应分离；普通名称只能在无歧义时解析；资源路径必须被根能力约束；隐式调用不是授权来源。

### 2.2 OpenCode

OpenCode从用户目录、项目向上发现目录、配置目录、显式路径和远端索引发现`SKILL.md`。目录阶段会读取完整内容并按名称写入Map，同名Skill记录告警但后加载项覆盖前项。模型先看到名称、描述和位置摘要，调用`skill` Tool并通过Permission后才获得正文和抽样文件列表。

远端Skill按`index.json`声明的版本写入缓存；版本变化使用临时目录、备份、原子重命名和失败清理。当前实现未要求每个文件带内容摘要，下载URL与路径也没有形成Harnessix所需的不可变供应链合同。Plugin使用宿主进程内动态导入，适合OpenCode扩展生态，但不适合作为Harnessix不受信扩展默认边界。

**可吸收结论**：Skill摘要与正文渐进加载、缓存版本原子切换具有产品价值；同名后写覆盖、无内容摘要远端分发和宿主内动态导入不能直接采用。

### 2.3 Claude Code逆向仓库

该仓库的Skill Frontmatter包含描述、版本、模型、可用Tool、路径、Hook和执行上下文等字段，并支持文件Skill、插件Skill和MCP Skill。正文加载时向模型提供Skill根路径，部分Skill还可触发Shell插值或注册Hook。

**可吸收结论**：丰富元数据便于产品表达，但Skill正文、`allowed-tools`、Hook或Shell字段均来自内容包，不能自动提升指令优先级、注册执行能力或扩大Secret/Sandbox权限。

## 3. Hook参考实现事实

### 3.1 Codex

Codex Hook覆盖`PreToolUse`、`PermissionRequest`、`PostToolUse`、会话、压缩、子Agent、停止和中断等事件。配置包含事件、Matcher、超时和处理器；有Matcher语义的事件才参与匹配。匹配处理器可并发执行，Permission Hook总等待上界取匹配处理器最大超时。

Hook运行结果区分Completed、Failed、Blocked和Stopped。命令Hook具有独立进程运行器、输出解析和超时；MCP Hook把就绪、Policy、超时和交互职责委托给MCP调用方。插件变更流程会在物化后保存可信Hook Hash，说明可执行配置变化必须使既有信任失效。

### 3.2 Claude Code逆向仓库

Hook配置在启动或显式修改时捕获快照。Managed Policy可以禁用全部Hook或仅允许Managed Hook；普通设置不能关闭Managed Hook。异步Hook进入注册表，保存进程、开始时间、超时和输出交付状态；收尾会等待完成或终止进程，并用`allSettled`隔离单个回调失败。

**共同结论**：Hook必须有不可变配置快照、来源优先级、精确匹配、独立超时、取消收尾和明确的失败/阻断结果；可执行定义变化必须撤销旧信任。

## 4. Harnessix现有能力与差距

0.7.5已经提供`TrustedActionRouter`与最小权限`ExtensionActionPort`，冻结Tool版本、输入Schema、资源、Policy、Workspace、Sandbox、Secret版本、审批和UNKNOWN恢复。0.8.4证明动态MCP Tool也只能通过此端口执行。仍缺少：

1. 多来源Skill目录、版本与内容摘要合同；
2. 同名冲突和限定名称解析；
3. 只暴露摘要、按需加载正文及资源的渐进加载；
4. Skill根防符号链接、硬链接、越界和读取竞态；
5. Hook定义摘要、显式信任授权和变更失效；
6. 生命周期匹配、顺序、超时、取消、失败和重启恢复；
7. Hook执行不能绕过Tool、Policy、Sandbox或读取原始Secret的结构性保证。

## 5. 设计结论

1. Skill是内容包，不是Python/JavaScript插件。0.8.5不加载第三方模块，不执行Skill脚本，不解释正文中的Tool、Hook、Shell或权限声明。
2. Skill来源只允许宿主显式绑定的`bundled`、`user`和`workspace`本地Root。远端市场、自动更新和签名分发留给后续供应链切片。
3. 目录快照只暴露限定名称、普通名称、描述、声明版本、来源和内容摘要；正文与资源只通过`skill`来源的Trusted Action按需读取。
4. 普通名称仅在全目录唯一时可用；跨来源同名必须使用`source/name`，单一来源内部重复名称使全部重复项失效，禁止后写覆盖。
5. 每次正文或资源读取都重新使用跨平台句柄链核对Root、路径类型和内容摘要。符号链接、Windows Reparse Point、硬链接、特殊文件、敏感名称、越界、超限和目录漂移均失败关闭。
6. Skill目录、访问结果和失败码进入独立私有SQLite；不保存正文、资源内容、绝对Root或Secret。
7. Hook只支持声明式生命周期绑定，不接受任意宿主Shell、HTTP、Prompt或进程内回调。处理器必须是宿主注册的`source="hook"`只读Trusted Action，并由对应`ExtensionActionPort`计划和执行。
8. 非Bundled Hook必须持有绑定完整定义摘要的显式Trust Grant；事件、Matcher、顺序、超时或Action指纹任一变化都会使授权失效。
9. Hook输入只传递Thread、Turn、目标Action身份和参数/结果摘要，不传原始参数、模型正文、环境或Secret。Hook返回的`allow`只是建议，不能修改目标Action的Policy、Sandbox或审批结论；`deny`可以收紧执行。
10. `before_action`采用串行确定顺序、Blocking和Fail Closed；其他0.8.5生命周期事件采用Advisory和Record Only。超时会取消底层Action并持久结算；宿主重启后遗留Running Hook收敛为Interrupted，不自动重放。

## 6. 源码索引

- Codex Skills：[`loading.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/skills/src/loading.rs)、[`selection.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/skills/src/selection.rs)、[`invocation.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/skills/src/invocation.rs)
- Codex Hooks：[`registry.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/hooks/src/registry.rs)、[`dispatcher.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/hooks/src/engine/dispatcher.rs)、[`command_runner.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/hooks/src/engine/command_runner.rs)
- OpenCode Skills：[`skill/index.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/skill/index.ts)、[`skill/discovery.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/skill/discovery.ts)、[`tool/skill.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/tool/skill.ts)
- Claude Code行为佐证：[`loadSkillsDir.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/skills/loadSkillsDir.ts)、[`hooksConfigSnapshot.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/utils/hooks/hooksConfigSnapshot.ts)、[`AsyncHookRegistry.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/utils/hooks/AsyncHookRegistry.ts)
