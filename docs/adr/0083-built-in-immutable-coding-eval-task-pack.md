---
doc_type: adr
status: current
version: 3
code_revision: 17e20691cf38c5dd1e2130de5f31c002dd6ac261
owners:
  - core
modules:
  - evals
  - product_config
  - sandbox
related_adrs:
  - docs/adr/0044-coding-eval-contract-and-grader.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
related_tests:
  - tests/evals/test_task_pack.py
  - tests/integration/test_task_pack_profiles.py
supersedes: []
---

# ADR-0083：采用内置不可变Coding Eval Task Pack与固定无网检查Profile

## 状态

接受且已实施。0.9.2b任务来源、工作区物化和真实检查已由CI 35461708961验收并关闭。0.9.2c可恢复Suite Runner
由ADR 0084独立定义并已形成实现候选；本文不关闭0.9.2d十任务三仓库数据集或0.9.2e真实Provider基线。

## 背景

0.9.2a已经定义跨任务Suite与Transcript证据，但原有Historical Catalog仍把单个Harnessix任务、专用Python检查器和
宿主入口耦合在一起。若直接接受运行时URL、仓库内测试命令或任意镜像，Eval输入即可把网络、命令注入、供应链漂移
和宿主代码执行引入质量门禁；若只复制目录而不固定Git身份，又无法证明不同Run使用同一基线。

[源码研究](../research/eval-suite-and-transcript-baseline.md)表明，Codex基准显式声明运行制品和数据，OpenCode的离线
回归仍装配真实运行链，Claude Code逆向样本只可交叉佐证失败关闭的Sandbox边界。三者都不构成直接复制其合同的理由，
因此Task Pack格式、来源权利链和物化语义由Harnessix独立定义。

## 决策驱动因素

1. 每个任务必须固定来源Commit、Tree、Archive、允许修改路径、检查、预算和许可证；
2. 安装后的Wheel必须离线携带同一资源，不能依赖运行时下载；
3. 模型和调用方只能选择固定Profile，不能提供程序、参数、镜像、网络或Secret；
4. Archive解析必须拒绝路径穿越、符号链接、设备和其他特殊成员；
5. 崩溃重开不能覆盖Agent已产生的未提交修改；
6. Linux真实容器检查与macOS本地物化必须使用同一Git身份算法；
7. 0.9.2b种子包只证明机制和双语言纵向链，不能冒充最终规模基线。

## 候选方案

| 方案 | 优点 | 关键缺陷 | 结论 |
|---|---|---|---|
| 运行时克隆任意URL并执行仓库命令 | 接入快 | 网络、分支和命令均可漂移，权利链不可控 | 拒绝 |
| Manifest接受宿主脚本路径 | 灵活 | 绕过Product Process与Container安全边界 | 拒绝 |
| 外部目录形式Task Pack | 易调试 | 调用方可伪造资源根或替换Archive | 拒绝v1公共入口 |
| Wheel内置版本目录、严格Manifest和固定Profile | 离线、可审计、可复现 | 每次更新需发新版本 | 采用 |
| 仅保存Golden Patch | 判定简单 | 误惩等价实现，不能证明Agent过程 | 拒绝 |

## 决策

Harnessix Task Pack v1遵循以下不可违反的不变量：

1. 公共加载入口只接受代码Catalog中的`pack_id + pack_version`，不接受路径、URL、命令或动态插件；
2. 每次消费`LoadedCodingEvalTaskPack`前重新加载内置Catalog并核对资源根和完整Manifest，公开数据类不构成信任凭据；
3. Manifest、Archive和许可证均为普通文件，禁止符号链接；Archive摘要、字节数、文件数和许可证摘要必须一致；
4. Archive只允许规范POSIX相对路径、`0644/0755`普通文件或`0755`目录，拒绝绝对路径、`..`、`.git`、Link和特殊成员；
5. 物化器使用固定作者、邮箱、时间、提交消息、`core.autocrlf=false`和无签名提交，随后同时核对Commit、Tree OID、
   原始`git ls-tree -r -z --full-tree`摘要和文件数；
6. `run_root`为`0700`，其下直接挂载的`workspace`为`0755`，Manifest为`0600`。父目录保持宿主私有，Workspace允许
   固定非root容器用户读取`0644`文件；
7. 相同Run ID已存在时只严格重开并核对HEAD身份，不清理或覆盖Agent工作树；HEAD变化、Manifest漂移或权限变化失败关闭；
8. 固定Profile必须使用Digest镜像、绝对容器程序、固定参数、无选择器、无Secret、`network=none`及有界CPU/内存/PID/
   时间/输出；宿主只绑定已存在的普通可执行Container Engine；
9. Review任务必须绑定版本化确定性Oracle，非Review任务禁止携带该Oracle；
10. Pack内容变化必须发布新`pack_version`，不得原地替换已发布版本。

## 理由

Catalog身份和内容摘要解决“选择了哪个任务”，Git四重身份解决“物化后是否仍是同一来源”，Product Process Profile
解决“检查实际如何执行”。三层校验互不替代。资源根在消费点重新核验，是因为Python公开数据类可由任意调用方构造；
仅在加载时验证会把一个可伪造对象误当作能力凭据。

工作区采用`0700/0755`分层而非全部`0700`：Linux Docker以`65532:65532`运行固定检查，直接bind mount的目录若为
`0700`则无法读取。其父`run_root=0700`仍阻止其他宿主用户沿路径访问，容器只获得Workspace只读挂载，不获得父目录、
Manifest或宿主写权限。

## 后果

### 正面后果

- Wheel离线安装后仍可重建完全相同的单提交Git基线；
- 任务来源、许可证、镜像和检查参数均可审计；
- 模型不能把任意Shell、网络或Secret注入测试链；
- 重开保留脏工作区，后续Suite Runner可在不重放副作用的前提下恢复；
- Python与JavaScript种子任务通过同一产品Trusted Action/Approval/Artifact链验收。

### 负面后果与债务

- v1不支持用户动态导入Task Pack；可信发布流程和签名留待0.9.4；
- 任务更新需要重新计算Archive、Git和Pack摘要并发布新版本；
- Task Pack物化依赖POSIX no-follow、权限和目录fsync语义，Windows当前只支持合同读取，真实执行由0.9.5另行关闭；
- 两个自研种子仓库只证明0.9.2b机制，不满足0.9.2d的十Case、三仓库和五类均衡要求。

## 兼容、安全与运维影响

现有Historical Eval、Campaign、Suite合同均不改版本；Task Pack新增三个v1 Schema和公开Python API。固定Profile复用现有
`ProductProcessProfile`、Trusted Action审批、Process Owner、只读Container、输出Artifact和Action Audit，不新增独立
HTTP/Worker服务。CI必须预拉Manifest中相同Digest的Node与Python镜像，运行时禁止`pull`和网络。

Task Pack目录属于发行供应链资源。运维不得手工修改安装目录；需要修订时生成新版本、重新构建Wheel并保留旧版本证据。

## 验证方式

- 严格合同、规范排序、摘要、双语言、Profile/Repository/Task绑定和Review Oracle正反测试；
- 伪造`LoadedCodingEvalTaskPack`、资源根符号链接、Archive篡改、路径穿越、Link和特殊成员拒绝测试；
- 两个Archive均精确重建单提交Commit、Tree、清单摘要、文件数和许可证；
- 同Run脏工作区重开不覆盖，Manifest、权限和HEAD漂移失败关闭；
- Wheel内容检查确认Manifest和两个Archive被打包；
- CI真实Docker链确认两个Baseline先失败，应用唯一允许路径中的最小修复后通过，并经过正式审批和Artifact投影。

实现Revision `608c07a54543f436651aa4e55141acb7f76021fc`已由
[CI 35461708961](https://github.com/carrie1988/Harnessix/actions/runs/35461708961)完成Linux Python 3.12/3.13、macOS、Windows、
固定镜像Container和Documentation六实例验收。固定镜像Job中的双语言Task Pack Profile真实执行通过；
首次Documentation的一次Mermaid冷启动超时已由同一Revision失败Job重跑的44幅变化图全部渲染通过排除内容错误。

## 关联资料

| 类型 | 路径 | 关系 |
|---|---|---|
| 详细设计 | [0.9.2详细设计](../changes/m09-2-eval-suite-and-transcript-baseline.md) | 字段、流程和失败语义 |
| 模块设计 | [Evals模块](../modules/evals.md) | 当前实现事实源 |
| Manifest | [`taskpacks/v1/manifest.json`](../../src/harnessix/evals/taskpacks/v1/manifest.json) | 内置Pack v1 |
| 合同 | [`task_pack_contracts.py`](../../src/harnessix/evals/task_pack_contracts.py) | Task、Profile、Oracle与物化合同 |
| 加载器 | [`task_pack.py`](../../src/harnessix/evals/task_pack.py) | Catalog、资源核验和Profile投影 |
| 物化器 | [`task_pack_materializer.py`](../../src/harnessix/evals/task_pack_materializer.py) | 安全解包、Git重建和恢复 |
| Runner决策 | [ADR 0084](0084-recoverable-sequential-eval-suite-runner.md) | Task Pack之上的顺序Suite恢复边界 |
| 单元测试 | [`test_task_pack.py`](../../tests/evals/test_task_pack.py) | 正反合同与恢复 |
| 真实验收 | [`test_task_pack_profiles.py`](../../tests/integration/test_task_pack_profiles.py) | 固定镜像产品运行链 |

## 被取代关系

无。ADR 0082继续拥有Suite分层与Transcript证据；本文细化其Task Pack和固定检查边界。
