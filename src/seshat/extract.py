"""Turning captured bytes into a readable witness. Spec §6.6.

Capture-once makes extraction failure permanent in one specific sense: the page
may be gone by the time anyone notices the parse was bad. The bytes are safe --
they are stored, and extraction re-runs over them -- but the *signal* that a
capture went wrong has to arrive while manual recovery is still possible, which
is what `status='thin'` is for.

Stdlib only, deliberately. `trafilatura` and `readability-lxml` are both
permissively licensed and better at this, but they pull lxml and a build
toolchain into what is otherwise a two-dependency project. The `extraction`
column records which extractor produced a row, so adopting a better one later
is a re-run over stored bytes -- exactly the path §6.6 designed for -- rather
than a migration or a loss.

Storing the raw HTML as the witness is not an option: that turns the store into
the document corpus §12 excludes. Stripping the advertising, tracking and
decoration leaves something far smaller than the page it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

EXTRACTOR = "stdlib-html/1"
"""Name and version recorded in `snapshot.extraction`. Bump when output changes,
so a re-run is distinguishable from an original capture."""

FETCH_PREFIX = "fetch:"
BROWSER_PREFIX = "browser:"
MANUAL = "manual"
"""§10.2 provenance, three-valued: `fetch` for the worker, `browser` for
extension captures, `manual` for pasted text. Pasted content is a witness too,
but a human-mediated one, and the distinction must survive in the data rather
than becoming invisible."""

TEXT_CAP = 64 * 1024
"""§6.6: 'a 64k cap covers articles comfortably'. `full_hash` is computed over
the complete extraction and `full_length` records what was cut, so truncation is
visible and cross-snapshot comparison still works when both copies were trimmed."""

THIN_CHARS = 200
"""§6.6: 'a 200 response yielding 200 characters is a failed parse, not a short
article'."""

THIN_RAW_FLOOR = 2000
"""...but only if the response was substantial to begin with. A genuinely tiny
page yielding tiny text is a short page, not a failed parse, and flagging it
would train the reviewer to ignore the flag. Both conditions must hold."""

# Elements whose text is never content. `nav`/`header`/`footer`/`aside` are the
# decoration §6.6 wants stripped; `script`/`style`/`template` are not prose.
_DROP = frozenset({
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select", "option",
    "figure", "figcaption", "picture", "video", "audio", "map", "object",
})
_BLOCK = frozenset({
    "p", "div", "section", "article", "main", "br", "hr", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "dl",
    "dt", "dd", "table", "tbody", "thead",
})

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_META_CHARSET = re.compile(rb"""charset=["']?\s*([A-Za-z0-9_\-]+)""", re.I)

_TEXTUAL_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
                  "+json", "+xml", "application/javascript")
_CONTROL = bytes(set(range(0, 32)) - {9, 10, 13})


@dataclass
class Extraction:
    """The derived half of a snapshot. `text` is already capped; `full_length`
    is the length before capping."""

    title: str | None
    text: str
    full_length: int
    extractor: str
    truncated: bool = False
    unsupported: bool = False
    """The bytes are not text this extractor can read -- a PDF, an image, a
    video. Not a parse failure to retry, but a capture a human must complete by
    pasting (§10.2), so it is surfaced as `thin` regardless of size."""


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._parts: list[str] = []
        self._drop_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._drop_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP:
            # Unbalanced markup is common; never let the counter go negative or
            # everything after a stray close tag disappears.
            self._drop_depth = max(0, self._drop_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title and self.title is None:
            stripped = data.strip()
            if stripped:
                self.title = stripped
            return
        if self._drop_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        joined = _WS.sub(" ", joined)
        joined = "\n".join(line.strip() for line in joined.split("\n"))
        return _BLANKS.sub("\n\n", joined).strip()


def decode(body: bytes, content_type: str | None) -> str:
    """Bytes to text, best effort and never raising.

    Charset from the header if present, from a `<meta charset>` otherwise, and
    UTF-8 with replacement as the floor. A mangled character is a far better
    outcome than a failed capture nobody can re-take.
    """
    charset = None
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    if not charset:
        match = _META_CHARSET.search(body[:4096])
        if match:
            charset = match.group(1).decode("ascii", "replace")
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def is_textual(content_type: str | None, body: bytes) -> bool:
    """Is this something the extractor can honestly read?

    A PDF decoded with errors="replace" yields tens of kilobytes of mojibake
    that looks like a successful extraction and is stored as the witness -- a
    silently corrupt record of what a source said, which is worse than an
    obvious failure. So media types are refused outright, and anything with a
    high control-byte density is treated as binary whatever it claims to be.
    """
    if content_type:
        lowered = content_type.lower()
        if any(marker in lowered for marker in _TEXTUAL_TYPES):
            return True
        return False
    sample = body[:4096]
    if not sample:
        return False
    control = sum(sample.count(bytes([b])) for b in _CONTROL)
    return control / len(sample) < 0.05


def looks_like_html(content_type: str | None, body: bytes) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    if content_type:
        return False
    return b"<html" in body[:2048].lower() or b"<!doctype html" in body[:2048].lower()


def extract(body: bytes, content_type: str | None = None) -> Extraction:
    """Extract readable text. Never raises: a note's witness must not depend on
    a parser surviving whatever the web served."""
    if not is_textual(content_type, body):
        return Extraction(
            title=None, text="", full_length=0, extractor=EXTRACTOR, unsupported=True
        )
    text_in = decode(body, content_type)
    title = None
    if looks_like_html(content_type, body):
        reader = _Reader()
        try:
            reader.feed(text_in)
            reader.close()
            text_out, title = reader.text(), reader.title
        except Exception:
            # Malformed beyond HTMLParser's tolerance: keep the decoded bytes
            # rather than losing the capture entirely.
            text_out = _BLANKS.sub("\n\n", _WS.sub(" ", text_in)).strip()
    else:
        text_out = _BLANKS.sub("\n\n", _WS.sub(" ", text_in)).strip()

    full_length = len(text_out)
    truncated = full_length > TEXT_CAP
    return Extraction(
        title=title,
        text=text_out[:TEXT_CAP],
        full_length=full_length,
        extractor=EXTRACTOR,
        truncated=truncated,
    )


def is_thin(extracted_length: int, raw_length: int, unsupported: bool = False) -> bool:
    """Did extraction plausibly fail? (§6.6)

    Unsupported media is always thin: there is text in that PDF, this extractor
    just cannot reach it, and a human can.

    Otherwise both conditions must hold: almost no text out, and a substantial
    response in. A short page really is short, and flagging it would train the
    reviewer to ignore the flag; a 50KB response yielding 40 characters is a
    paywall interstitial, a JS shell, or a cookie wall -- and needs a human
    while the target may still be live.
    """
    if unsupported:
        return True
    return extracted_length < THIN_CHARS and raw_length >= THIN_RAW_FLOOR


# --------------------------------------------------------- §6.9 expectation

EXPECTATION_COVERAGE = 0.5
"""Fraction of an expectation's content words that must appear in the
extraction for it to count as met. A guess, like every threshold in this design
that has not been measured -- and one that only feeds a review queue, so being
wrong costs a reviewer's glance rather than a corrupted record."""


def expectation_terms(expectation: str | None) -> list[str]:
    """Content words of an expectation, function words removed.

    Anchor text is short, so a single shared stopword would otherwise be a
    large fraction of the match -- the same failure §6.8 records for query
    expansion, in a different place.
    """
    from .store import STOPWORDS

    if not expectation:
        return []
    words = re.findall(r"[A-Za-z0-9_]+", expectation.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def expectation_met(expectation: str | None, text: str | None) -> bool | None:
    """Does the extraction contain what the citation said it would? (§6.9)

    Lexical, deliberately: embedding a whole page and comparing it to a short
    label is the mean-pooling blur that made chunking necessary for documents,
    and those cosines need corpus calibration besides.

    Returns None when the question cannot be asked -- no expectation recorded,
    nothing extracted yet, or an expectation made entirely of function words.
    None is not False, and the difference matters: one is "not checked", the
    other is "checked and failed".

    **Never a verdict.** A statistical table may contain none of the expected
    words in prose while being a perfect capture. Like §7.2's suggested links
    this is candidate generation for review, and it must never set `status`.
    """
    terms = expectation_terms(expectation)
    if not terms or not text:
        return None
    haystack = text.lower()
    found = sum(1 for term in terms if term in haystack)
    return (found / len(terms)) >= EXPECTATION_COVERAGE


def choose_expectation(
    label: str | None, desc: str, sentence: str | None = None
) -> tuple[str | None, str | None]:
    """What was sought at this link, and how that was decided (§6.9).

    The markdown anchor text states the expectation in the act of citing, and
    costs no new write-path argument. Where the label is missing or degenerate
    ("here", "this article"), the note's own desc is a better answer, optionally
    with the sentence the link sat in.

    The source is returned and stored because a failed check means different
    things depending on it: against a real anchor label the author said what
    they wanted and did not get it, while against a desc fallback the
    expectation was inferred and a miss may say nothing at all.
    """
    from .snapshots import DEGENERATE_LABELS

    cleaned = (label or "").strip()
    if cleaned and cleaned.lower() not in DEGENERATE_LABELS:
        return cleaned, "anchor"
    if sentence and sentence.strip():
        return f"{desc} -- {sentence.strip()}"[:500], "sentence"
    return (desc or None), ("desc" if desc else None)
