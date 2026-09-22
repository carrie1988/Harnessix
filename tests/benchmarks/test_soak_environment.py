from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from scripts.soak_environment import SoakEnvironment, read_environment


def test_environment_is_positive_and_contains_no_host_identity() -> None:
    environment = read_environment()

    expected = {"linux": "linux", "darwin": "macos", "win32": "windows"}
    assert environment.platform == expected[sys.platform]
    assert environment.cpu_count > 0
    assert environment.physical_memory_bytes > 0
    assert environment.hardware_class.startswith(f"c{environment.cpu_count}-m")
    assert set(environment.model_dump()) == {
        "platform",
        "python_version",
        "cpu_count",
        "physical_memory_bytes",
        "hardware_class",
    }


def test_environment_rejects_unexpected_identity_fields() -> None:
    data = read_environment().model_dump()
    with pytest.raises(ValidationError):
        SoakEnvironment.model_validate({**data, "hostname": "private-host"})
    with pytest.raises(ValidationError):
        SoakEnvironment.model_validate({**data, "physical_memory_bytes": 0})
