from __future__ import annotations

import subprocess

from harnessix.sandbox.capabilities import probe_host_sandbox


def test_host_sandbox_probe_only_advertises_backend_after_preflight() -> None:
    observed = []

    def failed(argv, timeout):
        observed.append((tuple(argv), timeout))
        return subprocess.CompletedProcess(argv, 1, "", "denied")

    probe = probe_host_sandbox(runner=failed)
    assert probe.guarded_available
    assert not probe.sandboxed_available
    assert probe.sandboxed_backend is None
    assert probe.backend_identity is None
    if observed:
        assert observed[0][1] == 5.0
        assert "preflight_failed" in probe.reason_code
