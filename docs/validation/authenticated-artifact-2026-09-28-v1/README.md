---
doc_type: validation-evidence
status: current
version: 1
code_revision: 33a2fd25bf6f529d1019cf584e02673734369299
owners: [core]
modules: [artifacts, session, secrets]
related_adrs:
  - docs/adr/0105-authenticated-artifact-body.md
related_tests:
  - tests/artifacts/test_authenticated_body.py
  - tests/agent/test_authenticated_store.py
supersedes: []
---

# Artifact原正文持久来源认证验证报告

## 1. 范围与固定版本

源码提交为`33a2fd25bf6f529d1019cf584e02673734369299`。
本目录记录Migration 0030新行来源认证、同Key跨重启读取、旧行与篡改拒绝、当前Secret再验证、
相关回归和完整测试的固定结果。正文、凭据与Key不进入证据包。

本机环境为macOS arm64、Python 3.13.8。真实模型请求0次，远程中间件调用0次。
固定源码Commit仅含19个显式源码/测试路径；未版本化工作区文件不进入构建与验收。
完整回归命令显式忽略`tests/security/`，该路径不计入通过数量。

## 2. 设计与现行边界

[总体与详细设计](../../changes/m09-4a-authenticated-artifact-body.md)、
[源码研究](../../research/authenticated-artifact-body.md)和[ADR-0105](../../adr/0105-authenticated-artifact-body.md)
定义原行签发、用途域HMAC、当前公开许可、迁移和维护取舍。
旧无Seal正文不补签；Windows原生、Key保护备份、全部Provider出口、0.9整体及正式商用发布仍开放。

六文件证据包括[合同事实](contract-facts.json)、[执行验证](verification.json)、
[Manifest](bundle-manifest.json)、[Review Packet](review-packet.json)及[CI观察](ci-observation.json)。
四幅新增架构/时序/读取/数据流程图已单独渲染PNG并逐图可视检查；仓库文档门禁另在
21个变更路径范围内启用Mermaid实际渲染通过，不能把结构检查冒充视觉验收。

## 3. 验证结果

- 固定源码完整本地回归：**5245通过、38跳过、0失败**；运行前后2449个已跟踪文件的整体SHA-256一致。
  `tests/security/`被显式排除，未运行、未暂存、未打包、未计入通过。
- 11项新Artifact来源合同通过：真实Runtime/SQLite只读归档、同Key新进程、新Secret命中旧安全正文拒绝、
  原Scope对象错配拒绝、迁移29旧行不补签、Seal/正文/Manifest/时间/Scope篡改、超限BLOB在Python读取前有界。
- 相关Artifact、Session升级、默认Root及Action Review回归在完整套件中执行；
  同事务回滚还沿用原[`test_authenticated_store.py`](../../../tests/agent/test_authenticated_store.py)故障点。
- 固定`git archive`建立Wheel与sdist；Wheel包含Migration 0030。独立Python 3.13虚拟环境按锁定依赖安装Wheel，
  两个真实OS进程分别产生和恢复Artifact，行摘要未变、Epoch确实变化，分页和历史引用验证通过。
  该环境复用本机下载/安装能力，不是干净机器或三平台正式安装验收。
- Ruff格式与静态检查、结构可读性、类型、文档及变更图渲染、合同与Task Pack、SBOM和发行物Secret扫描均通过。
  `license_scan --check`仍因**12件Archive来源权利**返回1；不报告`make check`整体通过。

## 4. CI、失败与发布边界

源码与文档在本地批量提交后只推送一次；不逐提交等待CI。
固定源码本地成功不代替精确最终提交的Linux、Windows和macOS作业结果，
CI观察状态记录在独立JSON。Windows原生Key及Artifact完整链尚未作为当前版本验收。
旧无Seal正文、Key保护备份/维护CLI、Owner同步阻塞、全部Provider/SDK/MCP出口、编号攻击、
正式安装/Beta/真实费用与许可来源链仍未完成，0.9.4a及总体0.9均保持开放。
