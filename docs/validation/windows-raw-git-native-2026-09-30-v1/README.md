---
doc_type: validation-evidence
status: current
version: 1
code_revision: ece88ade3e00532e3c4fa93d4abfb694401f13e4
owners: [core]
modules: [processes, tools, product_config]
related_adrs:
  - docs/adr/0097-typed-binary-publication-and-owner-protection.md
related_tests:
  - tests/processes/test_windows_raw_receipt.py
  - tests/processes/test_raw_output_receipt.py
  - tests/tools/test_windows_git.py
  - tests/product_config/test_git_baseline.py
  - tests/product_config/test_git_delivery_source.py
supersedes: []
---

# 固定候选Windows原生raw观察与测试合同复核

## 1. 当前结论

固定`ece88ade3e00532e3c4fa93d4abfb694401f13e4`的
[CI 36736381341](https://github.com/carrie1988/Harnessix/actions/runs/36736381341)
已实际执行新增19个raw/Git原生用例，全部通过。
活动根保护及退出后新根拒绝所在的首组128通过、2跳过，原Git/取消组166通过、5跳过、16排除。

**第三组整体失败：232通过、1失败、2错误、5跳过；后继重启/备份/恢复步骤跳过。**
19个新用例的通过与第三组失败同时保留，不称全Job或R4已通过。
[完整实现设计](../../changes/m09-r4-authenticated-raw-git-observation.md)
及[根目录生命周期设计](../../changes/m09-r4-product-git-delivery-source.md#9-windows原生根目录生命周期验证)
是对应源码及安全合同的事实源。

## 2. 实际原生覆盖

19个新用例包括真实CRT的LF/CRLF/Ctrl-Z/非UTF8、跨认证块秘密替换、原始预算停止、
受保护大blob、摘要用途Index/Status、九种解析元数据改写拒绝、历史v1拒绝、
超时及三种取消回收。全部实际Selector和原日志摘要见[事实](facts.json)。
这些用例属于第三组232项通过，不额外相加。
Windows Server Runner结果不替代Windows11消费者安装升级或独立Beta。

## 3. 两类失败与根因

1. 原64KiB+1的非法回执用例使用pytest自动参数名，名中含全部空格数据。
   setup和teardown将该名写入`PYTEST_CURRENT_TEST`，超过Windows环境变量32767字符限制。
   这是测试生命周期错误，Parser正文尚未执行；不能认定超限Parser已通过。
2. 合法8MiB来源/完整摘要已经通过；失败在附加的普通读端精确8MiB拒绝断言。
   原POSIX`CaptureProtocol`达到停止线即停止，原Windows Owner超过预算才停止。
   不能把前一端的比较条件当作后一端的既有合同，也不能放宽或统一生产预算以满足测试。

## 4. 修正与共同负控

[`回执测试`](../../../tests/processes/test_raw_output_receipt.py)只为七种非法输入指定短语义ID，
原非法字节和64KiB+1负载不删减，原Parser及64KiB上限不变。
收集所得最长Node ID仍远小于系统限制，结果见[测试记录](test-results.json)。

[`大blob测试`](../../../tests/product_config/test_git_baseline.py)保留8MiB完整摘要与1MiB前缀断言，
按原平台合同分别验证精确停止线；新增8MiB+1真实Git对象作为普通读端共同超限负控。
测试准备命令与受测只读端口分离，不扩大Patch输入、镜像容量或正式Git输出预算。
原8/9MiB预算、原比较条件、停止错误码和子进程回收均不改。

修正后macOS关联157通过，无跳过；该结果不代替修正后Windows原生全组。
原失败日志只保存于私有目录0700、原件0600；不把超长参数或原正文复制到公开材料。

## 5. 证据与未完成事项

[事实](facts.json)、[测试结果](test-results.json)、[Review Packet](review-packet.json)及
[Manifest](manifest.json)绑定实际候选、Selector、原日志及后继合同修正。
前序本机raw实现和安装材料见[独立交付包](../windows-raw-git-observation-2026-09-30-v1/README.md)，
不以其中本机通过覆盖本次原生失败。

生产raw实现没有因本次测试修正改写；没有模型请求、凭据读取或预算账本变更，R3成绩保持。
后继原生全组、消费者Windows11、正式Commit/Checkpoint接线、完整Git备份、独立Beta和R1～R6仍开放。
