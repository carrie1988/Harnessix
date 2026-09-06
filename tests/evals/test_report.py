from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.report import read_eval_report, write_eval_report
from tests.evals.test_grader import grade


def test_report_round_trip_uses_private_atomic_file(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    report = grade()
    write_eval_report(path, report)
    assert read_eval_report(path) == report
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_report_rejects_symbolic_link_and_invalid_body(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(target)
    with pytest.raises(KernelError, match="不能是符号链接"):
        write_eval_report(link, grade())
    with pytest.raises(KernelError, match="损坏"):
        read_eval_report(link)


def test_report_maps_missing_parent_to_public_write_failure(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as error:
        write_eval_report(tmp_path / "missing" / "report.json", grade())
    assert error.value.code == "eval_report_write_failed"
