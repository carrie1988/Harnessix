from __future__ import annotations

import sys

import pytest

from harnessix.agent.errors import KernelError
from scripts.soak_rss import _unit_from_probe, read_peak_rss


def test_unit_probe_distinguishes_bytes_and_kib_without_guessing() -> None:
    assert _unit_from_probe(80 * 1024 * 1024, 80 * 1024) == "bytes"
    assert _unit_from_probe(80 * 1024, 80 * 1024) == "KiB"
    with pytest.raises(KernelError) as error:
        _unit_from_probe(0, 80 * 1024)
    assert error.value.code == "soak_rss_unit_unknown"
    with pytest.raises(KernelError):
        _unit_from_probe(10 * 80 * 1024, 80 * 1024)


def test_current_platform_returns_positive_verified_peak_and_consistent_units() -> None:
    observation = read_peak_rss()

    assert observation.raw_value > 0
    assert observation.rss_bytes > 0
    assert observation.unit_verified
    if observation.raw_unit == "KiB":
        assert observation.normalization == "kib_times_1024"
        assert observation.rss_bytes == observation.raw_value * 1024
    else:
        assert observation.normalization == "identity"
        assert observation.rss_bytes == observation.raw_value
    if sys.platform == "linux":
        assert (observation.source, observation.raw_unit) == ("getrusage", "KiB")
    elif sys.platform == "darwin":
        assert observation.source == "getrusage"
    elif sys.platform == "win32":
        assert (observation.source, observation.raw_unit) == ("GetProcessMemoryInfo", "bytes")
