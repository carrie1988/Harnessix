---
doc_type: validation-evidence
status: current
version: 3
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [session]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_git_prefix_publication.py
  - tests/session/test_git_publication.py
  - tests/session/test_publication_seal.py
  - tests/agent/test_authenticated_store.py
  - tests/artifacts/test_authenticated_body.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_state_restore.py
supersedes: []
---

# Git尾锚端口验证命令与原记录定位

## 1. 历史实际命令

历史argv、解释器定位、临时目录及异常栈保留在受控归档，不在正式资料公开。有限定位见[ARCHIVE_REFERENCES.json](ARCHIVE_REFERENCES.json)；[commands.jsonl](support/commands.jsonl)保留逐条标签、UTC时间、退出码和原记录行号，其原成员SHA256可核对，未把模板冒充历史原命令。

## 2. 可移植复验模板

从仓库根目录运行，使用已准备的Python 3.12环境。原验证为Python 3.12.7，原bundled Git版本与二进制SHA256见[INPUTS.json](INPUTS.json)。不安装依赖、不改环境锁文件。临时证据目录由复验宿主独占创建，不进入产品持久化。

```bash
umask 022
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src:$PWD"
PYTHON="${PYTHON:-.venv/bin/python}"
SCRATCH=$(mktemp -d)
"$PYTHON" -m pytest -p no:cacheprovider tests/session \
  tests/agent/test_authenticated_store.py tests/artifacts/test_authenticated_body.py \
  tests/product_config/test_product_state_backup.py tests/product_config/test_product_state_restore.py \
  --basetemp="$SCRATCH/fixtures" --junitxml="$SCRATCH/session-related.xml"
"$PYTHON" -m ruff check --no-cache \
  src/harnessix/session/git_prefix_contracts.py src/harnessix/session/git_prefix_publication.py \
  src/harnessix/session/store_publication.py tests/session/test_git_prefix_publication.py
"$PYTHON" -m ruff format --check --no-cache \
  src/harnessix/session/git_prefix_contracts.py src/harnessix/session/git_prefix_publication.py \
  src/harnessix/session/store_publication.py tests/session/test_git_prefix_publication.py
"$PYTHON" -m mypy --cache-dir="$SCRATCH/mypy-production" \
  src/harnessix/session/git_prefix_contracts.py src/harnessix/session/git_prefix_publication.py \
  src/harnessix/session/store_publication.py
"$PYTHON" -m mypy --follow-imports=silent --cache-dir="$SCRATCH/mypy-test" \
  src/harnessix/session/git_prefix_contracts.py src/harnessix/session/git_prefix_publication.py \
  src/harnessix/session/store_publication.py tests/session/test_git_prefix_publication.py
"$PYTHON" scripts/documentation_check.py --root . --format json
```

以上是可移植模板，不是原命令。版本3实际运行正式RED/GREEN、尾锚152、原关联530、peer原文件10PASS及Ruff/format/mypy；原v1与版本2记录不改写。精确实际argv、环境和路径仅在受控closure-v3归档，公开[命令结果](support/closure-v3-commands.jsonl)以标签/UTC/退出码/原行号与成员SHA定位。

## 3. 外部独立用例文件复跑

外部测试路径会影响pytest配置发现；必须明确`-c pyproject.toml`使用本项目asyncio配置，不能以默认strict模式中的未执行async用例判定生产结果。`PEER_TEST_FILE`由受控归档中SHA固定的原文件提供，不修改内容：

```bash
"$PYTHON" -m pytest -c pyproject.toml -p no:cacheprovider "$PEER_TEST_FILE" \
  --basetemp="$SCRATCH/peer-fixtures" --junitxml="$SCRATCH/peer.xml"
```

此复跑由修复开发侧执行，不代替新一轮独立审查。签发图本次重新本地渲染PNG/SVG并目视核验；其余三图无需重新渲染。
