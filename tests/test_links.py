"""Link extraction from note text. Spec §6.4, §6.5."""

import pytest

from seshat import ids
from seshat.links import Link, extract, strip_code_fences


def targets(text, kind=None, **kw):
    return [l.target for l in extract(text, **kw) if kind is None or l.kind == kind]


def test_markdown_link_captures_anchor_text(store):
    """`[label](url)` is preferred precisely because the anchor text survives."""
    links = extract("See [the nomic card](https://ollama.com/library/nomic-embed-text) for dims.")
    assert links == [Link("url", "https://ollama.com/library/nomic-embed-text", "the nomic card")]


def test_bare_url_is_extracted_without_a_label():
    links = extract("Reference: https://sqlite.org/fts5.html covers the ranking function.")
    assert links == [Link("url", "https://sqlite.org/fts5.html", None)]


def test_a_target_is_not_reported_twice():
    text = "[a](https://example.com/x) and again https://example.com/x"
    assert targets(text) == ["https://example.com/x"]


def test_sentence_punctuation_is_not_part_of_the_url():
    assert targets("Look at https://example.com/page.") == ["https://example.com/page"]
    assert targets("(see https://example.com/page)") == ["https://example.com/page"]
    assert targets("Is it https://example.com/a?b=1, maybe?") == ["https://example.com/a?b=1"]


def test_balanced_parens_inside_a_url_survive():
    """Wikipedia targets genuinely contain them; trimming blindly breaks them."""
    url = "https://en.wikipedia.org/wiki/Bilinear_transform_(signal_processing)"
    assert targets(f"see {url} for the derivation") == [url]


def test_fenced_code_blocks_are_skipped():
    """A URL in a code sample is an example, not a reference (§6.5)."""
    text = """Real reference: https://example.com/real

```sh
curl https://example.com/in-a-fence
```

Still real: https://example.com/also-real
"""
    assert targets(text) == ["https://example.com/real", "https://example.com/also-real"]


def test_tilde_fences_and_mismatched_markers():
    text = "~~~\nhttps://example.com/fenced\n~~~\nhttps://example.com/open\n"
    assert targets(text) == ["https://example.com/open"]


def test_an_unclosed_fence_swallows_the_rest():
    """CommonMark closes an unterminated fence at end of document, so anything
    after the opener is code. Fewer links, never an error (§6.4)."""
    text = "before https://example.com/a\n```\nafter https://example.com/b\n"
    assert targets(text) == ["https://example.com/a"]


def test_strip_code_fences_preserves_line_count():
    text = "a\n```\nb\nc\n```\nd"
    assert len(strip_code_fences(text).splitlines()) == len(text.splitlines())


@pytest.mark.parametrize("raw,expected", [
    ("sha256:" + "a" * 64, "sha256:" + "a" * 64),
    ("SHA256:" + "A" * 64, "sha256:" + "a" * 64),
    ("b" * 64, "b" * 64),
    ("c" * 40, "c" * 40),
])
def test_content_hashes_are_extracted_and_normalised(raw, expected):
    assert targets(f"artifact {raw} was the input", kind="hash") == [expected]


def test_hex_inside_a_url_is_not_a_separate_hash():
    """URLs are consumed first, so a hex path segment stays part of the URL."""
    url = "https://example.com/blob/" + "d" * 64
    links = extract(f"fetched {url}")
    assert [l.kind for l in links] == ["url"]


def test_a_four_word_id_in_prose_is_an_internal_reference(store, mk):
    other = mk("a note worth pointing at")
    links = extract(f"This supersedes nothing but relates to {other} in spirit.")
    assert links == [Link("note", other, None)]


def test_hyphenated_prose_is_not_an_internal_reference():
    """The guard against dangling links: four hyphenated words only count when
    every one of them is really a BIP-39 word."""
    assert extract("a well-understood-but-annoying-quirk of the parser") == []
    assert extract("state-of-the-art results") == []


def test_a_real_four_word_english_phrase_that_happens_to_be_bip39():
    """Honest about the false-positive rate: this IS extracted, because every
    word is in the list. §6.5 accepts that in exchange for needing no markup."""
    phrase = "-".join(["black", "cat", "salt", "water"])
    assert all(w in ids.WORDS for w in phrase.split("-"))
    assert targets(f"the {phrase} problem", kind="note") == [phrase]


def test_self_reference_is_dropped(store, mk):
    note_id = mk("a note")
    assert extract(f"see {note_id} for context", exclude=note_id) == []


def test_filesystem_paths_are_not_extracted():
    """Too many false positives, and machine-local anyway (§6.5)."""
    assert extract("defined in /Users/x/src/seshat/store.py near the top") == []
    assert extract("see ./docs/spec.md and ~/notes/seshat.db") == []


def test_malformed_markdown_yields_fewer_links_never_an_error():
    """§6.4: the store never parses a note to decide whether to accept it."""
    for text in ("[unclosed](https://example.com", "[](", "](x)", "[a](  )", "```", "~~~~~"):
        extract(text)  # must not raise


def test_extraction_is_a_pure_function_of_text():
    """This is what makes DELETE FROM link a safe rebuild (§6.6)."""
    text = "[a](https://example.com/a) and " + "f" * 64
    assert extract(text) == extract(text)


def test_the_specs_own_example_id_is_not_a_valid_bip39_id():
    """§6.1 illustrates ids with `bright-otter-canvas-fig`, but `otter` and `fig`
    are not in the BIP-39 English list.

    Recorded as a test because it cost real debugging time: a dangling-link
    check appeared broken when it was correctly declining to treat a
    non-identifier as an identifier. Anyone reaching for the spec's example as
    test data will hit the same confusion.
    """
    assert "bright" in ids.WORDS and "canvas" in ids.WORDS
    assert "otter" not in ids.WORDS and "fig" not in ids.WORDS
    assert ids.canonicalize("bright-otter-canvas-fig") is None
    assert extract("see bright-otter-canvas-fig for the derivation") == []

    # A well-formed id made of real words is extracted.
    assert ids.canonicalize("olive-canvas-bright-zebra") == "olive-canvas-bright-zebra"
    assert targets("see olive-canvas-bright-zebra", kind="note") == ["olive-canvas-bright-zebra"]
