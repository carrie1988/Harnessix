---
doc_type: validation-evidence
status: current
version: 1
code_revision: f07263ce3d4ddb304b2ff054044f86f26d5267c6
owners: [core]
modules: [delivery, product_config, workspace]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_projection_ordering.py
  - tests/delivery/test_git.py
supersedes: []
---

# Git 派生事务顺序及私有来源解析验证

## 1. 结论与范围

**顺序冲突和私有来源解析缺口已由原组件及真实 Git 证明；新产品整改尚未实现。**

主仓固定基准 `f07263c` 的五项回归实际 5 通过、0 失败/错误/跳过，5.003 秒。
438 件生产 Python 源码、两件测试和两件配置共 442 件输入执行前后零漂移。
开发候选 `730f084` 实际工作文件的同一五项另行 5 通过，5.038 秒，477 件输入零漂移；
两组不得相加为十项产品验收或继承为未经运行的完整候选成绩。

正式背景、源码接口、状态表、四幅图、伪代码、失败及拟议整改见
[总体与详细设计](../../changes/m09-r4-git-projection-ordering.md)。
本次无生产源码、Bridge 校验、Schema、默认能力、依赖或期限变更。

## 2. 已证明事实

| 场景 | 实际结果 |
|---|---|
| 真实 detached 私有 A 上生成新 T 后，由原发布器发布 | 原 Store 回读为真正 `published`；A 的新增/删除/修改已物化；用户根 U 保持干净 |
| 发布后调用原 `plan_worktree(T,A)` | 原 `delivery_dirty_conflict`，未创建 D |
| 独立调用原 Snapshot 校验 | 原 `execution_plan_stale`，不是仅 Git status 校验失败 |
| 先使 D ready，再发布原来源，随后 Checkpoint | 仍原 `delivery_dirty_conflict`；无 Checkpoint 记录，D 保持基准内容 |
| 普通干净来源原组件正对照 | 原 T 保持 `prepared`，D 完整物化新增/删除/修改；另复用原 Commit 正对照证明确定性新 Ref 和来源保护 |
| 真实干净私有 A，不发布 T，调用旧 D 创建 | 规划成功，注册/checkout 后原回链绑定报 `git_worktree_binding_invalid`；直接只读原解析器报 `git_repository_changed`；实际 `creating` 效果保留 |

原 private-A 负对照不能被普通来源正对照替代；仅修正 `published` 前提也不能消除来源解析缺口。
测试没有替换 Publisher、Store、Git Runner、Root、Lease 或快照捕获器，没有手工推进事务状态。

## 3. 原失败保留与复验

开发候选首次四项为 3 通过、1 失败，3.157 秒；新测试错误期待内部解析错误，而原观察器实际包装为绑定错误。
仅修正新增测试的分层断言，并增加原解析器直接只读验证；生产源码、旧测试及批准策略未改。
原失败 XML、日志、测试源码、执行身份及 477 件输入锁定清单保留，不把它改写成生产修复的 RED。

命令以锁定依赖的 Python 3.12.7、Git 2.53.0、本机 macOS POSIX 执行；Workspace 夹具使用原 0644/0755 模式。

```bash
umask 022
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  -p no:cacheprovider -vv -s \
  tests/delivery/test_git_projection_ordering.py \
  tests/delivery/test_git.py::test_managed_worktree_checkpoint_and_commit_preserve_source
```

只有显式设置 `HARNESSIX_PROJECTION_ORDERING_EVIDENCE` 时，新测试才向已有私有目录创建事实文件；
目录须 0700，文件为 0600，不覆盖已有证据。普通回归不产生额外证据文件。

## 4. 资料与未验证边界

[verification.json](verification.json) 保存源码 SHA、原件 SHA、三次实际结果及覆盖范围。
私有交付目录另包含报告、Review Packet、输入锁、真实事务/工作树/Checkpoint 观察、源码快照和渲染图，
不公开临时根路径或完整运行日志。

本次不证明产品联合批准下的 T/Bridge/D、Checkpoint/Commit 已接通；不证明 Backup v2、
新根恢复、产品阶段取消/超时/硬退出恢复、Windows 原生、R3 编码质量、独立 Beta 或 R1～R6 发布。
未读取凭据、发送模型请求、修改费用或重跑 CI，默认完整 Git 能力仍关闭。
