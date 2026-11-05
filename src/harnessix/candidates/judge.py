"""交叉检查打平时的两两评审：同一对补丁按两种顺序各问一次，结论一致才算一票。

评审只看题面与两个补丁，不看隐藏测试；模型调用由调用方注入，便于替换为本地或云端模型。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations

from harnessix.candidates.selection import Candidate, Ranked

Ask = Callable[[str], str]

_PROMPT = """You review two candidate patches for the same issue. Decide which patch more likely
resolves the issue correctly and completely without breaking existing behaviour. Judge only from
the issue and the diffs.

Issue:
{issue}

Patch A:
{first}

Patch B:
{second}

Answer with exactly one letter: A or B."""

_ANSWER = re.compile(r"\b([AB])\b")


@dataclass(frozen=True)
class Judgement:
    first: str
    second: str
    forward: str | None  # A/B，按(first, second)顺序的回答
    backward: str | None  # 交换顺序后的回答
    winner: str | None  # 两种顺序一致时的胜者candidate_id，否则None


def _letter(answer: str) -> str | None:
    match = _ANSWER.search(answer.strip().upper()[:200])
    return match.group(1) if match else None


def judge_pair(issue: str, first: Candidate, second: Candidate, ask: Ask) -> Judgement:
    forward = _letter(ask(_PROMPT.format(issue=issue, first=first.patch, second=second.patch)))
    backward = _letter(ask(_PROMPT.format(issue=issue, first=second.patch, second=first.patch)))
    winner = None
    if forward == "A" and backward == "B":
        winner = first.candidate_id
    elif forward == "B" and backward == "A":
        winner = second.candidate_id
    return Judgement(first.candidate_id, second.candidate_id, forward, backward, winner)


def tied_leaders(ranking: Sequence[Ranked], candidates: Sequence[Candidate]) -> list[Candidate]:
    """与第一名在通过数、相同补丁数上并列且补丁内容不同的候选（每种补丁取排序靠前者）。"""

    if not ranking:
        return []
    best = ranking[0]
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    leaders, seen = [], set()
    for item in ranking:
        if (item.passed, item.duplicates) != (best.passed, best.duplicates):
            break
        candidate = by_id[item.candidate_id]
        if candidate.patch_sha256 not in seen:
            seen.add(candidate.patch_sha256)
            leaders.append(candidate)
    return leaders


def break_tie(
    issue: str, leaders: Sequence[Candidate], ask: Ask
) -> tuple[str | None, list[Judgement]]:
    """循环赛：一致判胜得一分；最高分唯一时返回其ID，否则返回None（保留原排序）。"""

    judgements = [
        judge_pair(issue, first, second, ask) for first, second in combinations(leaders, 2)
    ]
    wins = {candidate.candidate_id: 0 for candidate in leaders}
    for item in judgements:
        if item.winner is not None:
            wins[item.winner] += 1
    if not wins:
        return None, judgements
    top = max(wins.values())
    winners = [candidate_id for candidate_id, count in wins.items() if count == top]
    return (winners[0] if len(winners) == 1 and top > 0 else None), judgements
