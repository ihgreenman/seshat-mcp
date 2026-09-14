"""Markdown handling. Spec §6.4.

Note text is markdown *by convention, not by contract*: the store never parses a
note to decide whether to accept it, so everything here degrades to "returns
something reasonable" on malformed input and never raises.

The rule that is easy to get backwards: **embed stripped, index raw.** Markdown
syntax contributes tokens that are semantic noise -- URL fragments especially --
so markup is stripped before embedding, while FTS5 indexes the raw text, because
searching for a half-remembered URL is a real query.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)]*\)")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_BARE_URL = re.compile(r"https?://\S+", re.I)
_INLINE_CODE = re.compile(r"`+([^`]*)`+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET = re.compile(r"^\s{0,3}([-*+]|\d{1,9}[.)])\s+", re.M)
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", re.M)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S(?:.*?\S)?)\1", re.S)
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+.*$", re.M)
_SPACE = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def strip_code_fences(text: str) -> str:
    """Blank out fenced code blocks, preserving line structure.

    An unterminated fence runs to the end of the document, which is what
    CommonMark does. Lines are replaced rather than removed so anything
    reported by offset still lines up.
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
            if marker and marker.group(1)[0] == fence:
                fence = None
            out.append("")
    return "\n".join(out)


def strip_markup(text: str) -> str:
    """Reduce markdown to the prose an embedding model should see (§6.4).

    Anchor text is kept and link targets are dropped: `[the nomic card](https://…)`
    carries its meaning in the label, while the URL contributes tokens that
    push the vector toward other documents that merely cite similar hosts.
    Code blocks go entirely -- they are the single largest source of noise.
    """
    body = strip_code_fences(text)
    body = _REF_DEF.sub("", body)
    body = _IMAGE.sub(r"\1", body)
    body = _MD_LINK.sub(r"\1", body)
    body = _AUTOLINK.sub("", body)
    body = _BARE_URL.sub("", body)
    body = _INLINE_CODE.sub(r"\1", body)
    body = _RULE.sub("", body)
    body = _HEADING.sub("", body)
    body = _QUOTE.sub("", body)
    body = _BULLET.sub("", body)
    body = _EMPHASIS.sub(r"\2", body)
    body = _SPACE.sub(" ", body)
    body = _BLANKS.sub("\n\n", body)
    return body.strip()
