"""Text extraction from captured bytes. Spec §6.6.

The module is complete but NOT yet wired into the snapshot store -- spec 1.3
arrived mid-implementation and may change how extraction is staged. These tests
cover the extraction rules themselves, which are the part least likely to move.
"""

import pytest

from seshat.extract import (
    THIN_CHARS,
    Extraction,
    extract,
    is_textual,
    is_thin,
    looks_like_html,
)


def test_extracts_title_and_prose():
    html = b"<html><head><title>A Page</title></head><body><p>Real prose here.</p></body></html>"
    result = extract(html, "text/html")
    assert result.title == "A Page"
    assert result.text == "Real prose here."
    assert result.extractor.startswith("stdlib-html/")


def test_script_style_and_decoration_are_dropped():
    """§6.6 wants the advertising, tracking and decoration stripped -- storing
    raw HTML would make this the document corpus §12 excludes."""
    html = b"""<html><body>
      <nav>Home About Contact</nav>
      <script>var tracking = 1;</script>
      <style>.ad { display: block }</style>
      <p>The actual content.</p>
      <footer>Copyright notice</footer>
    </body></html>"""
    text = extract(html, "text/html").text
    assert "The actual content." in text
    for noise in ("Home About", "tracking", "display: block", "Copyright"):
        assert noise not in text, noise


def test_unbalanced_markup_does_not_swallow_the_page():
    """A stray close tag must not make everything after it vanish -- malformed
    HTML is the normal case, not the exception."""
    html = b"<html><body></script><p>Still here.</p></body></html>"
    assert "Still here." in extract(html, "text/html").text


def test_extraction_never_raises():
    for body in (b"", b"<", b"<html", b"\x00\x01\x02", b"<p>" * 10000):
        extract(body, "text/html")


def test_binary_media_is_refused_rather_than_mangled():
    """The bug this pins: a PDF decoded with errors='replace' yields tens of
    kilobytes of mojibake that looks like a successful extraction and gets
    stored as the witness. A silently corrupt record of what a source said is
    worse than an obvious failure."""
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 200
    result = extract(pdf, "application/pdf")
    assert result.unsupported is True
    assert result.text == ""
    assert is_thin(result.full_length, len(pdf), result.unsupported), (
        "there is text in that PDF; a human can reach it and this extractor cannot"
    )


def test_binary_is_detected_without_a_content_type():
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 200
    assert extract(pdf, None).unsupported is True
    assert is_textual(None, b"ordinary prose with no content type header") is True


@pytest.mark.parametrize("content_type,expected", [
    ("text/html; charset=utf-8", True),
    ("text/plain", True),
    ("application/json", True),
    ("application/pdf", False),
    ("image/png", False),
    ("video/mp4", False),
])
def test_textual_content_types(content_type, expected):
    assert is_textual(content_type, b"whatever") is expected


def test_charset_is_honoured():
    body = "café".encode("latin-1")
    assert "café" in extract(b"<p>" + body + b"</p>", "text/html; charset=latin-1").text


def test_meta_charset_is_used_when_the_header_is_silent():
    body = '<html><head><meta charset="latin-1"></head><body><p>café</p></body></html>'
    assert "café" in extract(body.encode("latin-1"), "text/html").text


def test_an_unknown_charset_falls_back_rather_than_failing():
    assert extract(b"<p>text</p>", "text/html; charset=x-nonexistent-99").text == "text"


# ------------------------------------------------------------------- thin


def test_a_big_response_yielding_nothing_is_thin():
    """§6.6: 'a 200 response yielding 200 characters is a failed parse, not a
    short article'. JS shells and paywall interstitials are the real cases."""
    shell = b"<html><head><title>App</title></head><body><div id=root></div><script>" \
            + b"var x=1;" * 3000 + b"</script></body></html>"
    result = extract(shell, "text/html")
    assert result.full_length < THIN_CHARS
    assert is_thin(result.full_length, len(shell)) is True


def test_a_genuinely_short_page_is_not_thin():
    """Both conditions must hold. Flagging every short page would train the
    reviewer to ignore the flag, which is worse than not having it."""
    page = b"<html><head><title>Note</title></head><body><p>Short.</p></body></html>"
    result = extract(page, "text/html")
    assert result.full_length < THIN_CHARS
    assert is_thin(result.full_length, len(page)) is False


def test_a_real_article_is_not_thin():
    article = b"<html><body><p>" + b"Sentences of real content. " * 200 + b"</p></body></html>"
    result = extract(article, "text/html")
    assert is_thin(result.full_length, len(article)) is False


# --------------------------------------------------------------- truncation


def test_truncation_records_what_was_cut():
    """§6.6: full_hash over the COMPLETE extraction and full_length recording
    what was cut, so truncation is visible and cross-snapshot comparison still
    works when both copies were trimmed."""
    from seshat.extract import TEXT_CAP

    body = b"<p>" + b"x" * (TEXT_CAP + 5000) + b"</p>"
    result = extract(body, "text/html")
    assert result.truncated is True
    assert len(result.text) == TEXT_CAP
    assert result.full_length == TEXT_CAP + 5000


def test_manual_extraction_is_a_distinct_provenance():
    """§10.2: pasted content is a witness too, but a human-mediated one, and
    the distinction must survive in the data rather than becoming invisible."""
    from seshat.extract import MANUAL

    pasted = Extraction(title="T", text="pasted body", full_length=11, extractor=MANUAL)
    assert pasted.extractor != extract(b"<p>x</p>", "text/html").extractor


def test_looks_like_html_prefers_the_header_over_sniffing():
    assert looks_like_html("text/html", b"not markup at all") is True
    assert looks_like_html("text/plain", b"<html><body>") is False
    assert looks_like_html(None, b"<!DOCTYPE html><html>") is True
