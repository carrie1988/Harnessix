---
doc_type: deployment-design
status: current
version: 1
code_revision: 2bd5846d15c59130f33055481cd73880d09b1d48
owners:
  - core
modules:
  - skills
  - product_config
related_adrs:
  - docs/adr/0074-skill-snapshot-and-hook-action-boundary.md
  - docs/adr/0125-skill-product-wiring.md
related_tests:
  - tests/skills/test_agent_tools.py
supersedes: []
---

# 使用 Skills

Skill 是一份写给 Agent 的任务说明，可以附带参考文件。Agent 启动时只看到名称和描述，任务相关时才读取全文，所以装多个 Skill 也不会占用多少上下文。

## 放在哪里

| 位置 | 用途 |
|---|---|
| `~/.harnessix/skills/<目录名>/SKILL.md` | 个人使用，所有仓库可见 |
| `<仓库>/.harnessix/skills/<目录名>/SKILL.md` | 随仓库共享给团队 |

请先运行一次 `harnessix code configure`，再创建个人目录。如果先建了目录，配置会因 `~/.harnessix` 权限过宽而失败，运行 `chmod 700 ~/.harnessix` 即可。

## 文件格式

```markdown
---
name: python-style
description: 这个仓库的 Python 约定。修改 Python 代码时使用。
version: 1.0.0
---

具体要求写在这里。需要时可以让 Agent 读取 references/banner.md。
```

- `name`：小写字母、数字以及 `.`、`_`、`-`，最多 64 个字符。
- `description`：最多 1024 个字符。Agent 靠它判断何时使用，请写清楚适用场景。
- `version`：可选。
- 正文最多 256 KiB。同一目录下的文本文件可以作为资源读取，最多 64 个，每个最多 64 KiB。

## 检查是否生效

```bash
harnessix skills list /path/to/repo
```

输出可用的 Skill，以及未被采用的文件和原因。

## 行为说明

- Skill 在 Agent 启动时读取一次。修改后要重启才生效；运行中改动的 Skill 会读取失败，不会读到一半新一半旧的内容。
- 个人和仓库里有同名 Skill 时，不会互相覆盖，需要用 `user/名称` 或 `workspace/名称` 指明。
- 还不支持远程安装、自动更新，也不能把 Skill 目录本身设为符号链接。

## 安全

Skill 只是文本，权限不会因此变化：

- Skill 里提到的脚本不会自动运行。Agent 要运行时仍走普通的命令工具和审批。
- `SKILL.md` 里声明的工具、权限或 Hook 都不生效。
- Agent 的文件修改和沙箱命令不能写入 `.harnessix` 目录，不能自己改写 Skill。经你批准的沙箱外命令不受此限，审批时请留意。
- 仓库里的 Skill 和 `AGENTS.md` 一样来自仓库内容。打开不信任的仓库前，先看看 `.harnessix/skills` 里写了什么。
