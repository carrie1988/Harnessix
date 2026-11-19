"""沙箱拦截了命令时给模型一句可照着做的提示（ADR 0176，Beta Issue #44后续）。

macOS Seatbelt与Linux bubblewrap里的命令不能写HOME等工作区以外的位置，也不能联网（按设计）。
首次运行Maven/Gradle Wrapper、安装依赖等命令因此以普通的非零退出失败，模型只看到报错，
不知道这是沙箱边界。只在本机沙箱里运行、非零退出且输出里有明确的拦截迹象时补一句说明；
普通的测试失败不加，免得模型动辄申请沙箱外执行。只读已通过保护与合同校验的预览。
"""

from __future__ import annotations

from pydantic import JsonValue

from harnessix.trusted_actions.contracts import ActionRoutePlan

# 写入被拒（Seatbelt报EPERM、只读绑定报EROFS）与联网失败的常见措辞，按小写子串匹配。
# 不含“permission denied”“connection refused”：普通失败里也常见，不能说明是沙箱所致。
DENIAL_SIGNATURES = (
    "operation not permitted",
    "read-only file system",
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "network is unreachable",
    "getaddrinfo enotfound",
    "getaddrinfo eai_again",
    "unknownhostexception",
    "failed to establish a new connection",
)

SANDBOX_DENIAL_HINT = (
    "The output suggests the sandbox blocked this command: sandboxed commands cannot write "
    "outside the workspace (for example to HOME, as a first-time download of a build tool or "
    "dependencies does) and have no network access. If the command needs that, rerun it with "
    "the command tool that runs outside the sandbox, if one is offered; that tool asks the user "
    "for approval. Do not try to work around the sandbox in other ways."
)


def sandbox_denial_hint(plan: ActionRoutePlan, output: JsonValue) -> str | None:
    """本机沙箱里的命令输出有拦截迹象时返回提示，否则返回None。"""
    if plan.execution.sandbox.level != "host_sandboxed" or not isinstance(output, dict):
        return None
    preview = output.get("diagnostic_preview")
    if not isinstance(preview, dict):
        return None
    streams = (preview.get("stdout"), preview.get("stderr"))
    texts = (stream.get("text") for stream in streams if isinstance(stream, dict))
    shown = " ".join(text for text in texts if isinstance(text, str)).lower()
    return SANDBOX_DENIAL_HINT if any(sign in shown for sign in DENIAL_SIGNATURES) else None
