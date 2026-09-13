"""Note identifiers: four BIP-39 English words, hyphenated. Spec §6.1.

Ids are word sequences because LLMs reproduce native tokens near-perfectly and
random base32 unreliably. Lookup is lenient (case, separators, four-character
word prefixes, one word-level edit); resolution is *never* silent.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from importlib.resources import files
from typing import Iterable, Sequence

WORD_COUNT = 4
"""Words per id. Not a tunable: §6.1's collision arithmetic assumes 4."""

RECOVERY_RADIUS = 1
"""Word-level Levenshtein radius for near-miss resolution.

Not a tunable either. A code corrects t errors iff d >= 2t+1; correcting two
needs d >= 5, but length 4 caps d at 4 by the Singleton bound.
"""


def _load_wordlist() -> tuple[str, ...]:
    text = files(__package__).joinpath("bip39_english.txt").read_text(encoding="utf-8")
    words = tuple(line.strip() for line in text.splitlines() if line.strip())
    if len(words) != 2048:
        raise RuntimeError(f"BIP-39 wordlist has {len(words)} words, expected 2048")
    return words


WORDS: tuple[str, ...] = _load_wordlist()
_WORD_SET = frozenset(WORDS)
# Every BIP-39 word is uniquely determined by its first four letters; this is
# one of the reasons the list is preferred over EFF diceware (§6.1).
_PREFIX4 = {w[:4]: w for w in WORDS}

_SEPARATORS = re.compile(r"[^a-z0-9]+")


class IdError(ValueError):
    """A requested id could not be turned into a candidate lookup key."""


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a caller-supplied id string.

    `by_proximity` is the flag that keeps §6.1's promise: a corrected id is
    always reported as corrected, never substituted silently.
    """

    id: str
    requested: str
    by_proximity: bool = False
    distance: int = 0


def new_id(rng: secrets.SystemRandom | None = None) -> str:
    """Mint a fresh id. Collision handling belongs to the caller (it needs the db)."""
    pick = (rng or secrets.SystemRandom()).choice
    return "-".join(pick(WORDS) for _ in range(WORD_COUNT))


def split(raw: str) -> list[str]:
    """Lenient tokenisation: case-insensitive, any separator."""
    return [t for t in _SEPARATORS.split(raw.strip().lower()) if t]


def canonical_word(token: str) -> str | None:
    """Expand one token to a BIP-39 word, accepting four-character prefixes."""
    if token in _WORD_SET:
        return token
    if len(token) >= 4:
        word = _PREFIX4.get(token[:4])
        if word is not None and word.startswith(token):
            return word
    return None


def canonicalize(raw: str) -> str | None:
    """Normalise a caller-supplied id to canonical form, or None if it is not one.

    Returns None for anything whose words are not all BIP-39 words (or unique
    prefixes of them), or whose word count is wrong. A None here is not yet a
    miss -- the caller falls through to proximity search, which is what lets a
    three- or five-word input resolve as a distance-1 candidate.
    """
    tokens = split(raw)
    if len(tokens) != WORD_COUNT:
        return None
    words = [canonical_word(t) for t in tokens]
    if any(w is None for w in words):
        return None
    return "-".join(words)  # type: ignore[arg-type]


def loose_words(raw: str) -> list[str]:
    """Best-effort word expansion for proximity search; unknown tokens pass through.

    Unexpandable tokens are kept verbatim so they still count as one edit away
    from the word they were meant to be, rather than dropping out of the
    comparison entirely.
    """
    return [canonical_word(t) or t for t in split(raw)]


def word_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Word-level Levenshtein distance.

    Levenshtein rather than Hamming because dropping a word (three instead of
    four) is a likelier generation error than substituting one, and a 3- or
    5-word input must read as distance 1 rather than as malformed (§6.1).
    """
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ai in enumerate(a, start=1):
        cur = [i]
        for j, bj in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ai != bj)))
        prev = cur
    return prev[-1]


def nearest(
    requested: str, known_ids: Iterable[str], radius: int = RECOVERY_RADIUS
) -> tuple[list[str], int | None]:
    """Find known ids within `radius` word-edits of `requested`.

    Returns (candidates, distance). `candidates` holds every id at the minimum
    distance found -- more than one means the request is ambiguous and must not
    be resolved. An empty list with distance None means nothing was within
    radius, which is §6.1's alarming case: a fabricated id rather than a
    corrupted one.
    """
    want = loose_words(requested)
    best: list[str] = []
    best_d: int | None = None
    for candidate in known_ids:
        have = candidate.split("-")
        if abs(len(have) - len(want)) > radius:
            continue
        d = word_distance(want, have)
        if d > radius:
            continue
        if best_d is None or d < best_d:
            best_d, best = d, [candidate]
        elif d == best_d:
            best.append(candidate)
    return best, best_d
