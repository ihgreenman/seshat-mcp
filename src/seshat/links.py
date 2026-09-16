"""Extraction of external references from note text. Spec §6.4, §6.5.

Link text is canonical; the `link` table is a derived index. Nothing here ever
rejects a note: extraction runs over whatever was written, and text that is not
valid markdown simply yields fewer links (§6.4). A write that failed over an
unbalanced bracket would violate priority 1 outright.

Everything in this module is a pure function of note text, which is what makes
`DELETE FROM link` a safe rebuild.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ids import WORD_COUNT, canonicalize

EXTRACTION_VERSION = "commonmark-ish/1"
"""Bump when the rules below change. A rebuild is a re-run, not a migration."""


@dataclass(frozen=True)
class Link:
    kind: str  # 'url' | 'hash' | 'note'
    target: str
    label: str | None = None


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?[^)]*\)")
# `)` is allowed through so the balance check in `_trim_url` can see it; a
# charset that excluded it would truncate Wikipedia targets before any trimming
# logic ran, which is the same bug in a place that looks safe.
_BARE_URL = re.compile(r"https?://[^\s<>\]\"'`]+", re.I)
_HASH = re.compile(
    r"\b(?:(sha256|sha512|sha1|blake3|md5)[:\-])?([0-9a-f]{64}|[0-9a-f]{40})\b", re.I
)
_MAYBE_ID = re.compile(r"(?<![\w-])([a-z]{3,8}(?:-[a-z]{3,8}){%d})(?![\w-])" % (WORD_COUNT - 1), re.I)

# Punctuation that ends a sentence rather than a URL. Balanced parens are left
# alone -- Wikipedia targets genuinely contain them.
_TRAILING = ".,;:!?'\"»”’"


def strip_code_fences(text: str) -> str:
    """Blank out fenced code blocks, preserving line structure.

    A URL in a code sample is an example, not a reference (§6.5) -- this is the
    direct payoff of the markdown convention in §6.4. Lines are replaced rather
    than removed so that anything reported by offset still lines up.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if fence is None:
            if marker:
                fence = marker.group(1)[0]
                out.append("")
                continue
            out.append(line)
        else:
            # A closing fence must use the same character as the opening one.
            if marker and marker.group(1)[0] == fence:
                fence = None
            out.append("")
    return "\n".join(out)


def _trim_url(url: str) -> str:
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    # Drop one unbalanced closing paren, the usual casualty of "(see http://x)".
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def extract(text: str, exclude: str | None = None) -> list[Link]:
    """Pull references out of note text (§6.5).

    Order matters: markdown links are consumed before bare URLs so a target is
    not reported twice, and URLs are consumed before hashes and note ids so a
    hex path segment or a hyphenated slug inside a URL is not mistaken for one.

    `exclude` drops a self-reference, which is noise rather than a link.
    """
    body = strip_code_fences(text)
    found: list[Link] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, target: str, label: str | None = None) -> None:
        key = (kind, target)
        if target and key not in seen:
            seen.add(key)
            found.append(Link(kind, target, label or None))

    def consume(pattern: re.Pattern[str], handler) -> None:
        nonlocal body
        pieces: list[str] = []
        end = 0
        for match in pattern.finditer(body):
            handler(match)
            pieces.append(body[end : match.start()])
            end = match.end()
        pieces.append(body[end:])
        body = " ".join(pieces)

    consume(_MD_LINK, lambda m: add("url", _trim_url(m.group(2)), m.group(1).strip()))
    consume(_BARE_URL, lambda m: add("url", _trim_url(m.group(0))))
    consume(_HASH, lambda m: add(
        "hash",
        f"{m.group(1).lower()}:{m.group(2).lower()}" if m.group(1) else m.group(2).lower(),
    ))

    for match in _MAYBE_ID.finditer(body):
        # A four-word run is only an id if every word is really a BIP-39 word;
        # ordinary hyphenated prose must not become a dangling internal link.
        note_id = canonicalize(match.group(1))
        if note_id is not None and note_id != exclude:
            add("note", note_id)

    return found


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def sentence_containing(text: str, target: str) -> str | None:
    """The sentence a link sits in, for §6.9's expectation fallback.

    Used only when the anchor text is missing or degenerate, so the cost of a
    crude sentence splitter is a slightly long expectation string rather than a
    wrong one.
    """
    for chunk in _SENTENCE.split(text.replace("\n", " ")):
        if target in chunk:
            cleaned = " ".join(chunk.split())
            return cleaned or None
    return None
