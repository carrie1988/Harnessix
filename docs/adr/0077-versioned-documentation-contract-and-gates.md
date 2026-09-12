---
doc_type: adr
status: current
version: 2
code_revision: 991b6f267671f5a86870672e9c97a5fbb3991a39
owners:
  - core
modules:
  - governance
  - documentation
related_adrs:
  - docs/adr/0076-code-readability-and-structural-governance.md
related_tests:
  - tests/governance/test_documentation_policy.py
  - tests/governance/test_generated_specs.py
supersedes: []
---

# ADR 0077：版本化文档合同与分层阻断门禁

## 状态

接受并已实施。DOC-1.6已按本决策交付版本化策略、检查器、反例测试、本地质量门和三平台CI。

## 背景

DOC-1.0～DOC-1.5已建立文档分类、模板、30份包级现行模块设计、Action Plane子系统设计、运维资料、
验证证据、里程碑历史、76份既有ADR和27份冻结源码研究。DOC-1.5结束时189份Markdown已经具备标准YAML元数据，
但当时仍依赖人工脚本验证元数据、链接、锚点、Mermaid、源码映射和敏感信息；本ADR及详细设计加入后的实施集合为191份。

只靠评审清单无法防止后续功能提交重新产生以下问题：文档状态自由文本化、源码链接漂移、模块实现变化而设计不更新、
重大合同变化没有变更设计、Mermaid在GitHub无法渲染，以及验证资料意外记录个人路径或凭据。

## 决策驱动因素

1. 默认离线、确定、跨macOS/Linux/Windows运行；
2. 单次执行应聚合报告全部问题，避免逐个修复；
3. 文档合同必须版本化，规则变化可评审、可测试；
4. 全库静态检查与提交差异检查职责分离；
5. Mermaid完整渲染依赖Node/Chromium，不应成为Python包运行依赖；
6. 门禁不得以空章节、关键词堆叠或机械注释替代设计质量；
7. 历史资料保持真实，不因现行模板升级被批量改写。

## 候选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 只依赖人工Review | 无实现成本 | 不可重复，无法阻断链接、元数据和同步退化 | 拒绝 |
| 引入通用Markdown/YAML/链接工具组合 | 生态成熟 | 多工具错误模型分散，难表达Harnessix生命周期和源码同步合同 | 部分采用；Mermaid复用官方CLI |
| 自研单一Python检查器，Mermaid渲染使用外部CLI | 统一合同、错误码和测试；无新增运行时依赖 | 需要维护Markdown子集解析与策略版本 | 接受 |
| 在应用Runtime内实现文档服务 | 可复用产品日志 | 污染生产包，扩大攻击面和安装体积 | 拒绝 |

## 决策

1. 新增版本化JSON策略`harnessix.documentation-policy/v1`，保存允许类型、状态、资源上限、结构要求、
   高风险变更模式和根级源码映射；
2. 新增仓库工具`scripts/documentation_check.py`，使用Python标准库和现有PyYAML依赖完成确定性检查；
3. 全库静态检查覆盖YAML、生命周期、路径、链接、标题锚点、索引覆盖、模块覆盖、源码/测试映射、
   Mermaid结构和敏感内容；
4. `--changed-from`单独检查源码与文档同步：生产包变化必须更新对应现行模块设计；跨包或合同、状态、
   持久化、安全边界变化必须同时提交`docs/changes/`变更设计；
5. 本地`make check`执行无网络静态门禁；CI文档任务安装固定版本Mermaid CLI，只渲染本次变化涉及的图；
6. 检查器输出稳定错误码、仓库相对路径、可选行号和中文解释；一次运行聚合全部错误并以非零退出；
7. 历史ADR和里程碑不强制套用现行详细设计章节，结构门禁只应用于现行模块设计和DOC-1后变更设计；
8. 规则实现必须有当前仓库正例和最小反例测试，不能通过关键词或关闭规则让回归“变绿”。
9. 公共Schema使用现有生成器在临时目录重建并逐字节校验当前生成集合；额外旧版本Schema作为兼容合同保留。

## 理由

Python检查器与项目现有工具链、三平台CI和测试框架一致；策略JSON使规则值与实现逻辑分离，避免把数量、类型和路径
散落在代码中。通用Markdown解析库会增加依赖和兼容面，而本门禁只需要仓库已采用的标题、链接和围栏代码块子集，
可通过有界解析和反例测试稳定维护。Mermaid语义解析不应伪造，因此完整语法证明交给固定版本官方CLI。

## 后果

### 正面后果

- 文档错误在提交前和CI中可重复发现；
- 生产源码变化与当前模块设计形成机械追踪；
- 重大协议、安全和跨包变化不能绕过变更设计；
- 文档工具不进入Harnessix运行时依赖和产品攻击面；
- Mermaid结构检查离线可用，完整渲染在CI中提供强证据。

### 负面后果与债务

- 自研解析器只支持仓库声明的Markdown子集，不等价于完整CommonMark实现；
- 差异检查依赖可解析的Git基线，新分支零SHA场景只能执行全库静态检查；
- 关键词结构检查只能证明章节存在，设计质量仍需评审与源码测试追踪；
- Mermaid CLI及其Chromium依赖增加文档CI时间，需要固定版本并与Python门禁隔离。

## 兼容、安全与运维影响

门禁不修改生产Schema、数据库或Runtime。检查器必须拒绝超大Markdown/Frontmatter、YAML重复键和Alias、
路径逃逸、符号链接文档及不可解析编码，避免CI资源耗尽或读取仓库外文件。错误输出不得复制疑似Secret正文。

CI使用只读仓库权限。Node/Mermaid只存在于文档任务，不写入Python锁文件或发行包；固定CLI版本升级必须通过独立变更评审。

## 验证方式

实施切片必须覆盖：当前191份资料全库通过；非法YAML、重复键、状态/版本/提交错误；链接和锚点缺失；
模块与测试映射缺失；个人路径、疑似凭据和过程性措辞；Markdown/Frontmatter预算；单包同步、跨包重大变更、
合同文件重大变更；Mermaid围栏、声明与外部渲染器失败。Linux、macOS和Windows至少执行离线门禁，Linux CI执行图表渲染。

## 实施结果

实现Revision `991b6f267671f5a86870672e9c97a5fbb3991a39`完成191份Markdown、4535个链接、483个Mermaid块和30个生产源码包的全库检查；22项文档检查器场景与3项合同生成场景覆盖正反边界。本地`make check`结果为`3351 passed, 13 skipped`，固定Mermaid CLI真实渲染通过。[CI 34709603781](https://github.com/carrie1988/Harnessix/actions/runs/34709603781)全部Job成功，其中Linux、macOS和Windows均通过离线文档门禁，Linux通过真实Mermaid渲染。公共合同生成在Linux/macOS通过；Windows因Evals现有POSIX `fcntl`依赖显式Skip，并保留为0.9.6平台债务。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 治理规范 | [文档工程规范](../governance/documentation-standard.md) | 元数据、结构与生命周期合同 |
| 整改计划 | [文档整改待办](../governance/documentation-remediation-backlog.md) | DOC-1.6范围和验收 |
| 详细设计 | [DOC-1.6详细设计](../changes/doc-1.6-automated-documentation-gates.md) | 组件、算法、错误与测试 |
| 前置决策 | [ADR 0076](0076-code-readability-and-structural-governance.md) | 代码治理和质量门原则 |

## 被取代关系

无。后续策略v2若改变门禁语义，应新增ADR并显式登记取代或扩展关系。
