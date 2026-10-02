---
doc_type: validation-evidence
status: current
version: 1
code_revision: dfba34e707ed845f3e9d844461e124015c22dca7
owners: [core]
modules: [delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_store_schema_v2.py
  - tests/delivery/test_git_store_readonly.py
  - tests/delivery/test_git.py
supersedes: []
---

# GitDB v2结构合同：集成与独立复核

## 1. 结论与边界

精确结构合同在集成候选实际460项通过，435个生产源码文件类型检查通过；
独立复核同冻结源正式460项与私有9项通过，25项输入零漂移，原359项断言语义保留。
同一新Wheel在macOS两个独立源码外Python环境各460项通过；不将限定安装验证外推为完整发布通过。

本切片仅增加唯一13表DDL、已有事务内helper与严格只读核验。
默认Store仍v1并拒绝v2；迁移、认证Writer/Loader、legacy全集、产品Git交付和Backup v2未完成。
Windows消费者、R3真实质量、独立Beta与商用门禁继续开放。

## 2. 设计、源码与冻结证据

- [总体与详细设计](../../changes/m09-r4-git-store-v2-schema.md)：完整字段、组合引用、
  18phase、事务/编码/错误合同及四张架构、流程、时序和数据流图。
- [原结构验证包](../git-store-v2-schema-2026-10-02-v1/README.md)：保持完整冻结，含原RED、
  四图实际渲染/视觉验收、正式UTF-16回归及原v1无漂移事实。
- [结构源码](../../../src/harnessix/delivery/git_store_schema_v2.py)与
  [正式测试](../../../tests/delivery/test_git_store_schema_v2.py)：精确SHA见[集成记录](INTEGRATION.json)。
- [当前Delivery模块](../../modules/delivery.md)与[路线图](../../roadmap.md)：当前部署与剩余产品范围。

原验证包以7bb为研究基线，新增两文件以固定SHA识别；本包以dfba为集成基线。
不将基线提交冒称已含新增文件，不改写原输入、失败或验证包。

## 3. 实际验证矩阵

| 层次 | 实际范围 | 结果 |
|---|---|---|
| 集成真实SQLite | 新363与原readonly85/Git12 | 460通过，0失败/错误/跳过 |
| 全生产源码类型 | 当前435文件，不是测试或三平台运行 | Mypy通过 |
| 源规范 | 两冻结文件 | Ruff/格式通过 |
| 独立复核 | 同一冻结两SHA，正式460及原私有9 | 469通过，0失败/错误/跳过 |
| 源码外安装 | 同一新Wheel、macOS Python3.12.7/3.13.8，原3选择器 | 各460通过、0失败/错误/跳过 |

各组460是不同执行，不合并制造新的用例数量。
唯一新Wheel SHA为`f094744fe3699fd417c4dcd78ce3b12543a80c5ba8e5b72d6a5b850b022c2810`；
476个当前生产包成员与实际Wheel全部名称和字节由集成侧逐一独立比较一致。
新独立环境沿用70项锁定依赖，不修改旧prefix Wheel或旧安装环境，不使用源码fallback。
原autoindex观察器假阳性FAIL保留；观察器修正不改变生产DDL、Schema版本或写入策略。

## 4. 复现与审查

在锁定开发环境、原测试依赖与固定Git支持存在时执行：

```bash
python -m pytest tests/delivery/test_git_store_schema_v2.py \
  tests/delivery/test_git_store_readonly.py tests/delivery/test_git.py
python -m ruff check src/harnessix/delivery/git_store_schema_v2.py \
  tests/delivery/test_git_store_schema_v2.py
```

实际测试解释器、原命令、XML/日志、完整输入与原文件摘要在受限审查归档中保存；
[INTEGRATION.json](INTEGRATION.json)仅发布有限聚合和归档/member/长度/SHA，不携带本机路径或正文。
普通文件摘要不代替业务来源MAC、Owner、Approval或执行能力。
[manifest.json](manifest.json)覆盖本包当前文件；原结构包manifest保持不变。

## 5. 保留证据与检查边界

原pytest日志及XML包含诊断原行的空白/缩进，完整diff-check保留原失败。
仅对`current-collection.log`、`final-all.log/.xml`、`implementation-01.log/.xml`和
`metadata-order-red.log/.xml`七个冻结证据文件作显式排除后，其他全部暂存源码、测试和文档diff-check通过。
不修改全局空白规则、不修剪历史诊断，也不将原FAIL改写为PASS。
复核边界见[Review Packet](review-packet.json)；完整原结果在受限审查归档保留。
