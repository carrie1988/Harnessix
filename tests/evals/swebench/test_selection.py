from __future__ import annotations

import random
from collections import Counter

import pytest

from harnessix.evals.swebench import select_swebench_cases


def dataset_rows(count: int = 40) -> list[dict[str, object]]:
    return [
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix public issue {index}",
            "version": f"{index % 3}.0",
            "issue_url": f"https://github.com/owner/repo/issues/{index}",
            "pr_url": f"https://github.com/owner/repo/pull/{index}",
            "patch": f"gold-{index}",
            "test_patch": f"tests-{index}",
            "FAIL_TO_PASS": f'["failed-{index}"]',
            "PASS_TO_PASS": f'["passed-{index}"]',
        }
        for index in range(count)
    ]


def test_selection_returns_exactly_25_unique_cases() -> None:
    selected = select_swebench_cases(
        dataset_rows(), salt="public-preregistration-salt-v1"
    )

    assert len(selected) == 25
    assert len({case.instance_id for case in selected}) == 25
    assert Counter(case.repo for case in selected) == {
        "owner0/repo0": 5,
        "owner1/repo1": 5,
        "owner2/repo2": 5,
        "owner3/repo3": 5,
        "owner4/repo4": 5,
    }
    assert {case.repo for case in selected} == {
        "owner0/repo0",
        "owner1/repo1",
        "owner2/repo2",
        "owner3/repo3",
        "owner4/repo4",
    }


def test_selection_is_stable_when_input_rows_are_reordered() -> None:
    rows = dataset_rows()
    reordered = rows.copy()
    random.Random(7).shuffle(reordered)

    assert select_swebench_cases(
        rows, salt="public-preregistration-salt-v1"
    ) == select_swebench_cases(reordered, salt="public-preregistration-salt-v1")


def test_evaluator_only_changes_do_not_change_selection() -> None:
    rows = dataset_rows()
    changed = [
        {
            **row,
            "patch": f"different-gold-{index}",
            "test_patch": f"different-tests-{index}",
            "FAIL_TO_PASS": f'["different-failed-{index}"]',
            "PASS_TO_PASS": f'["different-passed-{index}"]',
        }
        for index, row in enumerate(rows)
    ]

    assert select_swebench_cases(
        rows, salt="public-preregistration-salt-v1"
    ) == select_swebench_cases(changed, salt="public-preregistration-salt-v1")


def test_selection_fails_when_fewer_than_25_unique_candidates_exist() -> None:
    with pytest.raises(ValueError, match="at least 25"):
        select_swebench_cases(
            dataset_rows(24), salt="public-preregistration-salt-v1"
        )


def test_selection_rejects_duplicate_public_identity() -> None:
    rows = dataset_rows()
    rows[-1] = {**rows[0], "patch": "other gold"}

    with pytest.raises(ValueError, match="instance_id"):
        select_swebench_cases(rows, salt="public-preregistration-salt-v1")
