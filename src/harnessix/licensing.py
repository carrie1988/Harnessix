from __future__ import annotations

LICENSE_EXPRESSION = "AGPL-3.0-only"
COPYRIGHT_NOTICE = "Copyright © 2026 Zhang Jinhui"
SOURCE_URL = "https://github.com/carrie1988/Harnessix"
COMMERCIAL_LICENSE_URL = f"{SOURCE_URL}/blob/main/COMMERCIAL_LICENSE.md"


def render_license_notice() -> str:
    return "\n".join(
        (
            "Harnessix Code",
            COPYRIGHT_NOTICE,
            f"社区许可证：{LICENSE_EXPRESSION}",
            f"源代码：{SOURCE_URL}",
            f"商业许可：{COMMERCIAL_LICENSE_URL}",
        )
    )
