from __future__ import annotations

from harnessix.candidates.judge import break_tie, judge_pair, tied_leaders
from harnessix.candidates.selection import Candidate, Ranked

GOOD = Candidate("c0", "diff good\n", "completed")
BAD = Candidate("c1", "diff bad\n", "completed")
OTHER = Candidate("c2", "diff other\n", "failed")


def _prefers(patch: str):
    """评审桩：总是选择内容为patch的一方。"""

    def ask(prompt: str) -> str:
        first = prompt.split("Patch A:\n", 1)[1].split("\n\nPatch B:", 1)[0]
        return "A" if first.strip() == patch.strip() else "B"

    return ask


def test_a_vote_counts_only_when_both_orders_agree() -> None:
    assert judge_pair("issue", GOOD, BAD, _prefers(GOOD.patch)).winner == "c0"
    assert judge_pair("issue", BAD, GOOD, _prefers(GOOD.patch)).winner == "c0"
    # 位置偏好（总选A）在两种顺序下矛盾，不算票。
    biased = judge_pair("issue", GOOD, BAD, lambda prompt: "A")
    assert (biased.forward, biased.backward, biased.winner) == ("A", "A", None)
    assert judge_pair("issue", GOOD, BAD, lambda prompt: "no idea").winner is None


def test_round_robin_picks_a_unique_leader_or_abstains() -> None:
    winner, judgements = break_tie("issue", [BAD, GOOD, OTHER], _prefers(GOOD.patch))
    assert winner == "c0" and len(judgements) == 3
    assert break_tie("issue", [GOOD, BAD], lambda prompt: "A")[0] is None


def test_leaders_are_distinct_patches_tied_on_checks_and_agreement() -> None:
    twin = Candidate("c3", GOOD.patch, "completed")
    ranking = [
        Ranked("c0", 3, 1, True, 10),
        Ranked("c3", 3, 1, True, 10),
        Ranked("c1", 3, 1, True, 12),
        Ranked("c2", 2, 1, False, 9),
    ]
    leaders = tied_leaders(ranking, [GOOD, BAD, OTHER, twin])
    assert [candidate.candidate_id for candidate in leaders] == ["c0", "c1"]
