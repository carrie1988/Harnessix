from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "\\absolute",
        "C:/absolute",
        "C:relative",
        "../escape",
        "a/../escape",
        "a//b",
        "a/./b",
        "a\\b",
        "a\0b",
        "a\nb",
    ],
)
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_logical_paths_reject_platform_prefix_and_traversal(value, platform) -> None:
    with pytest.raises(KernelError) as error:
        normalize_workspace_path(value, platform)
    assert error.value.code == "workspace_path_denied"


@pytest.mark.parametrize(
    "value",
    [
        "CON",
        "con.txt",
        "NUL.json",
        "COM1",
        "lpt9.log",
        "CONIN$",
        "file:stream",
        "name.",
        "name ",
        "dir/AUX.txt",
        "COM¹.log",
        "lpt³",
    ],
)
def test_windows_rejects_reserved_ads_and_collapsed_names(value) -> None:
    with pytest.raises(KernelError) as error:
        normalize_workspace_path(value, "windows")
    assert error.value.code == "workspace_path_denied"


def test_windows_supports_long_logical_paths_without_legacy_260_limit() -> None:
    value = "/".join(["segment" * 10] * 8)
    assert len(value) > 260
    assert normalize_workspace_path(value, "windows") == value


def test_platform_comparison_key_only_folds_windows() -> None:
    assert path_comparison_key("Src/Main.py", "posix") != path_comparison_key(
        "src/main.py", "posix"
    )
    assert path_comparison_key("Src/Main.py", "windows") == path_comparison_key(
        "src/main.py", "windows"
    )


def test_unknown_platform_is_rejected_even_for_workspace_root() -> None:
    with pytest.raises(KernelError) as error:
        normalize_workspace_path(".", "unknown")  # type: ignore[arg-type]
    assert error.value.code == "workspace_platform_unsupported"
