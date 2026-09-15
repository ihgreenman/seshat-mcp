"""Identifiers: generation, lenient lookup, never-silent resolution. Spec §6.1."""

import itertools
import random

import pytest

import reference
from seshat import ids
from seshat.store import AmbiguousId, NoteNotFound


def test_wordlist_invariants():
    """The two properties §6.1 buys from BIP-39 over EFF diceware."""
    assert len(ids.WORDS) == 2048 == len(set(ids.WORDS))
    prefixes = [w[:4] for w in ids.WORDS]
    assert len(set(prefixes)) == 2048, "every word uniquely determined by 4 letters"


def test_ids_are_four_words():
    """Four, not three: at 10k notes a one-word corruption lands on a real note
    with P ~ 1/140 at three words and ~ 1/215,000 at four."""
    for _ in range(50):
        assert len(ids.new_id().split("-")) == 4


def test_word_distance_matches_naive_definition():
    """DP table vs. the textbook recursion -- two derivation paths, not one."""
    alphabet = ["alpha", "beta", "gamma"]
    cases = []
    for n in range(4):
        cases.extend(itertools.product(alphabet, repeat=n))
    rng = random.Random(0)
    for a, b in rng.sample(list(itertools.product(cases, cases)), 200):
        assert ids.word_distance(list(a), list(b)) == reference.levenshtein_naive(list(a), list(b))


def test_word_distance_recognises_deletion_as_one_edit():
    """Levenshtein, not Hamming: a dropped word is a likelier generation error
    than a substituted one, and 3- and 5-word inputs must read as distance 1."""
    full = ["bright", "otter", "canvas", "fig"]
    assert ids.word_distance(full, ["bright", "canvas", "fig"]) == 1
    assert ids.word_distance(full, ["bright", "otter", "zoo", "canvas", "fig"]) == 1
    assert ids.word_distance(full, ["bright", "otter", "canyon", "fig"]) == 1


def test_canonicalize_is_lenient_about_case_and_separators(store, mk):
    note_id = mk("a note")
    words = note_id.split("-")
    for candidate in (
        note_id.upper(),
        " ".join(words),
        "_".join(words),
        ".".join(words),
        f"  {note_id}  ",
    ):
        assert store.resolve(candidate).id == note_id
        assert store.resolve(candidate).by_proximity is False


def test_four_character_prefixes_resolve_exactly(store, mk):
    note_id = mk("a note")
    truncated = "-".join(w[:4] for w in note_id.split("-"))
    resolution = store.resolve(truncated)
    assert resolution.id == note_id
    assert resolution.by_proximity is False, "a prefix is exact, not a near miss"


def test_prefix_shorter_than_the_word_is_not_a_match(store):
    """'ab' could be abandon, ability, able, about, above, absent... -- refusing
    is the point, since a wrong expansion would be a silent substitution."""
    assert ids.canonical_word("ab") is None
    assert ids.canonical_word("abil") == "ability"
    assert ids.canonical_word("act") == "act", "3-letter words match exactly"


def test_single_substitution_resolves_but_is_flagged(store, mk):
    note_id = mk("a note")
    words = note_id.split("-")
    wrong = [w for w in ids.WORDS if w != words[2]][7]
    corrupted = "-".join([words[0], words[1], wrong, words[3]])

    resolution = store.resolve(corrupted)
    assert resolution.id == note_id
    assert resolution.by_proximity is True
    assert resolution.distance == 1
    assert resolution.requested == corrupted


def test_dropped_word_resolves_but_is_flagged(store, mk):
    note_id = mk("a note")
    words = note_id.split("-")
    resolution = store.resolve("-".join(words[:2] + words[3:]))
    assert resolution.id == note_id
    assert resolution.by_proximity is True


def test_read_surfaces_the_correction(store, mk):
    """Never substitute silently -- that is priority 2 (§1)."""
    note_id = mk("a note")
    words = note_id.split("-")
    corrupted = "-".join([words[0], "zoo" if words[1] != "zoo" else "zebra", words[2], words[3]])

    record = store.read(corrupted)
    assert record.id == note_id
    assert record.resolved_by_proximity is not None
    corrected = record.resolved_by_proximity["corrected"][0]
    assert corrected["requested"] == corrupted
    assert corrected["resolved_to"] == note_id


def test_fabricated_id_raises_and_is_recorded_as_such(store, mk):
    """§6.1's alarming case: nothing within distance means the id was invented,
    not mistyped, and the two must not look the same in the record."""
    mk("a note")
    with pytest.raises(NoteNotFound):
        store.resolve("zoo-zoo-zoo-zoo")

    row = store.db.execute("SELECT * FROM near_miss WHERE requested = ?", ("zoo-zoo-zoo-zoo",)).fetchone()
    assert row["candidate"] is None
    assert row["distance"] is None, "distance NULL distinguishes fabricated from ambiguous"
    assert row["hit_count"] == 1


def test_ambiguous_resolution_refuses_to_guess(store):
    """Two real notes one edit away. Picking either would be a coin flip
    presented as a lookup."""
    words = ids.new_id().split("-")
    base = store.db
    ids_made = []
    for tail in ("zoo", "zebra"):
        candidate = "-".join(words[:3] + [tail])
        base.execute(
            "INSERT INTO note(id, desc, text, created_at) VALUES (?,?,?,?)",
            (candidate, "d", "t", "2026-01-01T00:00:00.000000+00:00"),
        )
        ids_made.append(candidate)
    base.commit()

    requested = "-".join(words[:3] + ["zone"])
    with pytest.raises(AmbiguousId) as excinfo:
        store.resolve(requested)
    assert set(excinfo.value.candidates) == set(ids_made)

    row = store.db.execute("SELECT * FROM near_miss WHERE requested = ?", (requested,)).fetchone()
    assert row["candidate"] is None and row["distance"] == 1


def test_near_miss_caches_and_counts(store, mk):
    """The row doubles as a cache so the same corruption skips the scan -- but a
    cached candidate is still returned flagged, never promoted to a redirect."""
    note_id = mk("a note")
    words = note_id.split("-")
    corrupted = "-".join([words[0], words[1], words[2], "abandon" if words[3] != "abandon" else "zoo"])

    first = store.resolve(corrupted)
    second = store.resolve(corrupted)
    assert first.id == second.id == note_id
    assert second.by_proximity is True, "cached rows are never silent redirects"

    row = store.db.execute("SELECT * FROM near_miss WHERE requested = ?", (corrupted,)).fetchone()
    assert row["candidate"] == note_id
    assert row["hit_count"] == 2


def test_two_errors_are_not_recovered(store, mk):
    """Not a tunable: d >= 2t+1 needs d >= 5 for two errors, but length 4 caps
    d at 4 by the Singleton bound. Two-error correction is impossible here."""
    note_id = mk("a note")
    words = note_id.split("-")
    others = [w for w in ids.WORDS if w not in words]
    corrupted = "-".join([words[0], words[1], others[3], others[400]])

    with pytest.raises(NoteNotFound):
        store.resolve(corrupted)


def test_exact_hits_never_write_a_near_miss(store, mk):
    note_id = mk("a note")
    store.resolve(note_id)
    assert store.db.execute("SELECT COUNT(*) FROM near_miss").fetchone()[0] == 0


def test_minted_ids_do_not_collide(store):
    made = {store.create_note(f"n{i}", "body")[0] for i in range(200)}
    assert len(made) == 200


def test_id_resolution_never_consults_vector_similarity(store, mk, monkeypatch):
    """§6.1, spec 1.2: id resolution is lexical only.

    Word ids embed as roughly the average of four concepts, so two ids
    differing in one word land almost on top of each other. If near-miss
    recovery consulted embedding similarity it would confidently return *a
    different real note* in place of the clean not-found that §6.1's sparsity
    argument exists to guarantee.

    "Ids are words, and words embed fine" is a plausible and wrong inference,
    so this asserts the vector path is not reachable from resolution at all.
    """
    import seshat.vectors as module

    mk("a real note")

    def forbidden(*args, **kwargs):
        raise AssertionError("id resolution must not consult vector similarity")

    monkeypatch.setattr(module, "search", forbidden)
    monkeypatch.setattr(module, "similarity_for", forbidden)

    with pytest.raises(NoteNotFound):
        store.resolve("zoo-zoo-zoo-zoo")
    assert store.resolve(mk("another note")) is not None


@pytest.mark.parametrize("text,expected", [
    ("olive-canvas-bright-zebra", True),
    ("OLIVE CANVAS BRIGHT ZEBRA", True),
    ("zoo-zoo-zoo-zoo", True),
    ("notaword-notaword-notaword-notaword", True),
    ("why does my filter cutoff move", False),
    ("olive-canvas-bright", False),
    ("olive-canvas-bright-zebra-extra", False),
    ("sha256:abc123", False),
])
def test_id_shaped_queries_are_detected_by_shape_alone(text, expected):
    """§3.2's hint fires on shape, not validity -- a mistyped id is exactly when
    the caller most needs telling that context does not resolve identifiers."""
    assert ids.looks_like_id(text) is expected
