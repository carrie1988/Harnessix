---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: 80c1dad13c98aaabb2db1630c62134a241df44aa
owners: [core]
modules: [session, agent, artifacts]
related_adrs:
  - docs/adr/0103-authenticated-sqlite-session-commit.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_authenticated_history.py
  - tests/artifacts/test_batch_diff.py
supersedes: []
---

# 同事务认证历史：真实数据库回归与独立复核

## 1. 结论与边界

新增内部Reader在原单一SQLite只读事务中认证Thread投影和全部事件，完整重放相等后交付普通历史。
原CancelToken、绝对期限和Owner检查贯穿；没有新增Key、Schema、MAC域、公开Tool或执行授权。
开发候选正式62项及21个文件的780项关联回归通过，435个实际生产文件类型检查通过。
独立复核同冻结字节正式62及原私有8项通过，SH-PEER-01/02闭环。
各组重叠，不累加成全仓测试数；集成类型与源码外安装结果另行记录。

该结果不是跨库原子视图、当前Root或批准，也不关闭完整Git产品、Backup v2、R3、Windows或商用门禁。

## 2. 设计与源码导航

- [总体与详细设计](../../changes/m09-r4-authenticated-thread-history.md)：背景、四图、类/字段、伪代码、原期限、失败结算与兼容边界。
- [当前Session模块](../../modules/session.md)与[Agent模块](../../modules/agent.md)：部署和错误分类事实。
- [Reader实现](../../../src/harnessix/session/sqlite_history.py)、[原Store门面](../../../src/harnessix/session/sqlite.py)及[原事件认证](../../../src/harnessix/session/sqlite_publication.py)。
- [正式回归](../../../tests/session/test_authenticated_history.py)及[Artifact结构守卫](../../../tests/artifacts/test_batch_diff.py)。
- [精确源身份](source-sha256.json)、[实际结果与历史归档摘要](SUMMARY.json)、[评审包](review-packet.json)。

## 3. 验证矩阵与原失败

| 批次 | 实际范围 | 结果 |
|---|---|---|
| 正式历史读取 | 真实WAL双连接、MAC先解析、原限额、VM停止、Owner身份、回滚拒绝、父取消 | 62通过，0失败/错误/跳过 |
| 关联回归 | [原21个选择器](carrier-related-selectors.json)，包含上述62 | 780通过，0失败/错误/跳过 |
| 开发类型 | 实际435个生产源码文件 | Mypy通过；不是运行或三平台验收 |
| 独立复核 | 同一正式62及原Owner/驱动私有8 | 70通过，0失败/错误/跳过 |
| 源码外安装 | 同一新Wheel、macOS Python3.12.7/3.13.8、包含正式62的原29选择器 | 各1376通过，见[集成原件](../authenticated-product-integration-2026-10-02-v1/README.md)，非三平台 |

真实SQLite authorizer拒绝ROLLBACK，确认原稳定存储错误不被早先Owner错误覆盖。
Owner单独失败保留原对象；父取消展开和连接关闭沿用原资源结算。
未独立注入底层close驱动故障，该优先级依据未变资源代码和载体捕获边界；不声称全清理故障或硬实时。

初始接口/检查点命名、夹具错误及两轮P2负例保留原件。
旧757项的2项Artifact守卫FAIL在无新Reader的基线独立复现；整改只核验四个私有字段键和合法工具别名，
不改产品输出、别名、评分或审批。原57项Owner断言及全部业务守卫保留，新增5项真实清理/取消测试。
原件身份以SUMMARY的archive_id/member/sha256记录；历史FAIL不改写，既有签字不扩大范围。

## 4. 复现、图形与审查

在项目锁定环境执行：

```bash
python -m pytest tests/session/test_authenticated_history.py
python -m ruff check src/harnessix/session/sqlite_history.py \
  tests/session/test_authenticated_history.py tests/artifacts/test_batch_diff.py
```

关联批次使用JSON中全部选择器，不删失败选择器。实际XML与日志为原字节，不用摘要替代原始结果。
源码与安装输入必须来自同一冻结候选；禁止使用其他工作树安装包或源码fallback。
四个mmd与设计图块一致，PNG经Chrome实际渲染并视觉检查，最终像素及SHA见[图形审查](diagram-review.json)。
没有读真实Key、Keychain、历史评测Run或费用账本；只使用自有合成状态，未调用模型或Docker。

## 5. 发布条件

集成候选须继续完成实际类型、限定关联、文档/Secret及同一Wheel源码外验证，保留所有原失败和输入身份。
认证历史只是后继Loader与评测恢复的读取事实，不允许跳过当前Owner、新批准、对象目录或业务备份。
