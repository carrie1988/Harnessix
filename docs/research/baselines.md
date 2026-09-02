# 0.2 源码研究基线

- 状态：已冻结
- 冻结日期：2026-09-02
- 适用范围：Harnessix Code 0.2 架构研究

## 1. 基线提交

| 项目 | 本地目录 | 分支 | 提交 | 研究定位 |
|---|---|---|---|---|
| Codex | HARNESSIX_RESEARCH_ROOT/codex | main | [a0dcfe2](https://github.com/openai/codex/tree/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67) | Agent Loop、App Server、会话恢复、工具路由、执行策略和 Sandbox 的主要事实来源 |
| OpenCode | HARNESSIX_RESEARCH_ROOT/opencode | dev | [69c172e](https://github.com/anomalyco/opencode/tree/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5) | Provider 归一化、事件投影、Tool、Permission、HTTP/SSE 协议的主要事实来源 |
| Claude Code 逆向仓库 | HARNESSIX_RESEARCH_ROOT/claude-code-source-code | 本地当前分支 | [2ca5dda](https://github.com/carrie1988/claude-code-source-code/tree/2ca5ddabfed5f220812ea11f029eda03b21bc4c1) | Query Loop、Context 和 Tool 行为的辅助佐证 |

提交号使用完整 SHA 写入本文，后续即使参考仓库分支移动，0.2 结论仍可复查。

## 2. 证据等级

研究文档使用以下标签：

- **事实**：可从锁定提交的源码、测试或正式协议定义直接验证；
- **推断**：由多个调用点或用户行为归纳，源码没有直接声明；
- **决策**：Harnessix Code 的设计选择，不代表参考项目现状；
- **待验证**：需要在 0.3 及后续版本通过 Spike、故障注入或 Eval 验证。

不得把目录名、类型名或注释单独当作完整运行语义；关键结论至少要追踪到入口、状态转换和失败路径。

## 3. 使用限制

### Codex

Codex 是公开源码，结构完整，但内部协议和模块仍可能快速变化。研究结论只对应锁定提交，不把内部类型直接复制为 Harnessix 公共 API。

### OpenCode

锁定提交中的 V2 Session Runner 仍包含明确的未完成清单。本文同时观察其事件存储和 Provider 设计，但不会把迁移中的实现误写为稳定、完整的生产方案。

### Claude Code 逆向仓库

该仓库不是 Anthropic 官方源码，存在反编译命名、缺失模块和行为偏差。它只用于交叉验证可观察机制，不作为安全、协议或持久化决策的唯一依据，也不复制其实现。

## 4. Clean-room 规则

1. 只记录机制、边界、不变量、失败语义和工程权衡；
2. 不复制参考仓库的实现代码；
3. Harnessix 的领域命名和公共契约由本项目 ADR 独立定义；
4. 参考仓库不是 Harnessix 的构建、运行或测试依赖；
5. 后续若升级研究提交，新增基线版本，不静默覆盖旧结论。

## 5. 可复查命令

~~~bash
git -C "$HARNESSIX_RESEARCH_ROOT/codex" show -s --format='%H %cI %s' \
  a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67
git -C "$HARNESSIX_RESEARCH_ROOT/opencode" show -s --format='%H %cI %s' \
  69c172e8a7c0086887b1f93ed5a162f14b6aa0c5
git -C "$HARNESSIX_RESEARCH_ROOT/claude-code-source-code" \
  show -s --format='%H %cI %s' \
  2ca5ddabfed5f220812ea11f029eda03b21bc4c1
~~~

0.2 的最小验证是：锁定提交可读取、文档引用路径存在、调用链中的入口与终态可由静态源码复核。运行时语义不靠真实模型 API 验证；0.3 使用 Scripted Provider 和故障注入把这些结论转成可执行契约。
