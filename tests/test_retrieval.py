"""`context` behaviour: pool filtering, recency degradation, scoring, safety.
Spec §3.2, §5.1, §6."""

import pytest

from seshat.store import RRF_K, Store, fts_query


def test_empty_query_is_a_recency_listing(store):
    """§3.2: this is how session-start orientation works without a sixth tool."""
    made = [store.create_note(f"note {i}", f"body {i}")[0] for i in range(5)]
    hits = store.context("")
    assert [h.id for h in hits] == list(reversed(made))


def test_keyword_match_finds_body_and_desc(store, mk):
    a = mk("Bilinear transform loses precision near Nyquist", "prewarping is the fix")
    mk("Unrelated thing about gearboxes", "torque")
    assert [h.id for h in store.context("nyquist")] == [a]
    assert [h.id for h in store.context("prewarping")] == [a]


def test_notes_are_findable_immediately_after_writing(store, mk):
    """FTS5 is a synchronous trigger with no external dependency (§6.3), so a
    note is keyword-findable the moment it is written."""
    note_id = mk("Ahead-of-time compilation breaks the plugin loader")
    assert [h.id for h in store.context("plugin loader")] == [note_id]


def test_retracted_notes_leave_the_pool(store, mk):
    a = mk("Prewarping is unnecessary", "wrong")
    b, _ = store.create_note(
        "Prewarping is required after all", "right", [{"id": a, "retained": 0.0}]
    )
    found = {h.id for h in store.context("prewarping")}
    assert found == {b}
    assert a not in found


def test_additive_supersession_keeps_the_old_note_findable(store, mk):
    """§5.1's load-bearing consequence: at retained=1.0 the new note may hold
    only the addendum, so excluding the old one silently loses correct content."""
    a = mk("Nyquist precision loss in the bilinear transform", "the original finding")
    b, _ = store.create_note("Also affects the phase response", "addendum only",
                             [{"id": a, "retained": 1.0}])
    assert {h.id for h in store.context("nyquist")} == {a}
    assert {h.id for h in store.context("phase")} == {b}


def test_since_filters_by_creation_time(store, mk):
    early = mk("early note")
    cutoff = store.db.execute("SELECT created_at FROM note WHERE id = ?", (early,)).fetchone()[0]
    late = mk("late note")

    assert {h.id for h in store.context("", since=cutoff)} == {early, late}
    later = store.db.execute("SELECT created_at FROM note WHERE id = ?", (late,)).fetchone()[0]
    assert {h.id for h in store.context("", since=later)} == {late}


def test_limit_is_respected(store):
    for i in range(10):
        store.create_note(f"note {i}", "body")
    assert len(store.context("", limit=3)) == 3
    assert len(store.context("note", limit=4)) == 4


def test_scores_are_returned_and_strictly_decreasing(store):
    """§3.2: a bare list implies everything in it is relevant, and a reader will
    act accordingly. The score is what makes honest triage possible."""
    for i in range(4):
        store.create_note(f"note {i}", "body")
    hits = store.context("note")
    assert all(h.score > 0 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    # RRF shape, so the scale survives the vector side landing (§6).
    assert hits[0].score == pytest.approx(1.0 / (RRF_K + 1))


def test_context_returns_descriptions_never_bodies(store, mk):
    mk("recognisable handle", "a long body that must not be returned in triage")
    hit = store.context("recognisable")[0]
    assert hit.desc == "recognisable handle"
    assert not hasattr(hit, "text")


@pytest.mark.parametrize(
    "query",
    ['"', 'foo AND bar', 'NEAR(a b)', 'x OR', '*', 'col:val', "it's", "a -b", "()"],
)
def test_fts_syntax_in_a_query_cannot_break_retrieval(store, mk, query):
    """Callers pass free text, not FTS5 expressions. Every token is quoted."""
    mk("something about foo and bar", "col val near")
    store.context(query)  # must not raise


def test_query_with_no_usable_tokens_degrades_to_recency(store):
    made = [store.create_note(f"n{i}", "body")[0] for i in range(3)]
    assert [h.id for h in store.context("!!! ??? ---")] == list(reversed(made))


def test_fts_query_uses_or_for_recall():
    assert fts_query("nyquist precision") == '"nyquist" OR "precision"'
    assert fts_query("   ") is None


def test_unicode_survives_a_round_trip(store):
    note_id, _ = store.create_note("τ ≈ p − ½ for the monopole filter", "decay constant, not a time")
    assert store.read(note_id).desc == "τ ≈ p − ½ for the monopole filter"
    assert {h.id for h in store.context("monopole")} == {note_id}


def test_store_reopens(tmp_path):
    path = tmp_path / "persist.db"
    s = Store(path)
    note_id, _ = s.create_note("persisted", "body")
    s.close()

    s2 = Store(path)
    assert s2.read(note_id).desc == "persisted"
    assert [h.id for h in s2.context("persisted")] == [note_id]
    s2.close()


def test_rationales_are_searchable_but_never_in_context(store, mk):
    """§5.5: findable when looked for, never competing with notes in `context`."""
    a = mk("claim about resampling")
    b, _ = store.create_note("revised claim", "body",
                             [{"id": a, "retained": 0.3, "why": "polyphase decomposition changed it"}])
    assert store.search_rationales("polyphase")[0]["rationale"] == "polyphase decomposition changed it"
    assert store.context("polyphase") == []
