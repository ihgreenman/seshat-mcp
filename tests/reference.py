"""Independent reference implementations.

House rule: never compute a quantity by only one path. Nothing in here may
import from `seshat` -- these are derived from the spec text, not from the
code under test, so a shared bug cannot make a test pass green.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def latest_edges(assessments: Sequence[tuple[str, str, float, str]]) -> dict[tuple[str, str], float]:
    """Latest-wins resolution over (old, new, retained, asserted_at) rows (§5.5)."""
    best: dict[tuple[str, str], tuple[str, float]] = {}
    for old, new, retained, stamp in assessments:
        key = (old, new)
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, retained)
    return {k: v[1] for k, v in best.items()}


def retained_of(note: str, edges: dict[tuple[str, str], float]) -> float:
    """§5.1: 1.0 with no outgoing edges, else the maximum over DIRECT edges.

    Direct edges only -- no path composition, which is what makes the diamond
    resolve without traversal.
    """
    out = [r for (old, _new), r in edges.items() if old == note]
    return 1.0 if not out else max(out)


def pool(notes: Iterable[str], edges: dict[tuple[str, str], float], theta: float) -> set[str]:
    return {n for n in notes if retained_of(n, edges) >= theta}


def heads(note: str, edges: Iterable[tuple[str, str]]) -> set[str]:
    """Descendants (including self) with no outgoing edge. Breadth-first, no SQL."""
    edge_list = list(edges)
    seen = {note}
    frontier = [note]
    while frontier:
        cur = frontier.pop()
        for old, new in edge_list:
            if old == cur and new not in seen:
                seen.add(new)
                frontier.append(new)
    has_out = {old for old, _ in edge_list}
    return {n for n in seen if n not in has_out}


def levenshtein_naive(a: Sequence[str], b: Sequence[str]) -> int:
    """Textbook recursive definition -- exponential, correct, and not the DP
    table the implementation uses. Only called on tiny inputs."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    if a[0] == b[0]:
        return levenshtein_naive(a[1:], b[1:])
    return 1 + min(
        levenshtein_naive(a[1:], b),
        levenshtein_naive(a, b[1:]),
        levenshtein_naive(a[1:], b[1:]),
    )
