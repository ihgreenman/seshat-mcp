"""`context` behaviour: pool filtering, recency degradation, scoring, safety.
Spec §3.2, §5.1, §6."""

import pytest

from seshat.store import RRF_K, SeshatError, Store, fts_query
from seshat.worker import EmbeddingWorker


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


def test_function_words_do_not_retrieve_on_their_own(store):
    """The failure this guards, observed live: with OR expansion a single
    function word can retrieve a document at rank 1, and RRF -- being
    rank-based -- then treats that ranking as authoritative and can promote the
    note above a correct semantic match."""
    noise = store.create_note("Catastrophic cancellation in tail energy",
                              "total minus cumsum dies when the two are nearly equal")[0]
    wanted = store.create_note("Reciprocal rank fusion needs no score calibration",
                               "rank-based, so incommensurable scorers are never reconciled")[0]

    assert "two" not in fts_query("combining two rankings")
    assert noise not in [h.id for h in store.context("combining two rankings")]
    assert wanted in [h.id for h in store.context("reciprocal rank fusion")]


def test_a_query_of_only_function_words_still_searches(store):
    """Dropping every token would turn a real query into a recency listing,
    which is a different answer wearing the same shape."""
    note_id = store.create_note("counting", "one two three")[0]
    assert fts_query("one two three") == '"one" OR "two" OR "three"'
    assert [h.id for h in store.context("one two three")] == [note_id]


def test_content_words_are_kept_verbatim():
    assert fts_query("the bilinear transform") == '"bilinear" OR "transform"'
    assert fts_query("nomic-embed-text") == '"nomic" OR "embed" OR "text"'


def test_context_cannot_find_a_note_by_its_own_id(store, mk):
    """Documents a surprising, silent behaviour rather than endorsing it.

    `note_fts` indexes desc and text, and the embedding is desc + text, so a
    note's id appears in neither index. `context(<id>)` therefore cannot return
    the note with that id -- only notes whose text mentions it.

    Consistent with the spec: read() is the symbol-addressing path, and §6.1
    deliberately routes a badly mangled id to content search. But an assistant
    whose id is past the recovery radius may well try context() next and get
    nothing, with no hint that id lookup is not something context does.

    Open question in docs/retrieval-findings.md §3.1 -- if the spec decides
    context should resolve identifiers, this test changes with it.
    """
    # The id is pinned, not minted at random. A four-word BIP-39 id OR-expands
    # against FTS, so if any drawn word also appears in the note's own text the
    # note matches itself and this test goes red -- measured at 0.2% per run
    # against the wording below, which is ~1 red in 3 for every 200 CI runs.
    # An intermittent failure teaches people to re-run rather than to look.
    import seshat.ids as ids_module

    pinned = "abandon-ability-able-about"
    original = ids_module.new_id
    ids_module.new_id = lambda *args, **kwargs: pinned
    try:
        note_id = mk("Resampler drops a sample at block boundaries")
        assert note_id == pinned
        ids_module.new_id = original
        assert [h.id for h in store.context(note_id)] == []

        citing = store.create_note(
            "confirmed on hardware", f"reproduced what {note_id} describes"
        )[0]
        assert [h.id for h in store.context(note_id)] == [citing], "citations are findable"
    finally:
        ids_module.new_id = original


def test_the_cli_renders_a_recency_listing(store, capsys, tmp_path):
    """§3.2 made score/similarity/matched null on an empty query, and the CLI
    formatter assumed numbers. Found on the first real use of the store, which
    is exactly the path no unit test had exercised."""
    from seshat.cli import main

    path = tmp_path / "cli.db"
    s = Store(path)
    s.create_note("a finding", "body")
    s.close()

    assert main(["--db", str(path), "--no-snapshots", "--no-embeddings", "context"]) == 0
    out = capsys.readouterr().out
    assert "a finding" in out
    assert "recency" in out, "say which retriever ran, and none did"


# ------------------------------------------------ §3.2 `since` is a real bound


def test_an_unparseable_since_is_refused_rather_than_matching_nothing():
    """The comparison is a string comparison, so "yesterday" sorts above every
    real timestamp and the query confidently returns nothing. A caller reads an
    empty result as "the store holds nothing on this" -- a silent wrong answer,
    which is the failure class this store exists to prevent."""
    store = Store(":memory:")
    store.create_note("alpha", "a note about bilinear transforms")
    assert len(store.context("bilinear")) == 1

    for bad in ("yesterday", "last week", "18/09/2026", "next tuesday"):
        with pytest.raises(SeshatError, match="ISO 8601"):
            store.context("bilinear", since=bad)


def test_an_offset_bearing_since_compares_as_an_instant():
    """Validation alone would not have caught this one. A timestamp carrying a
    +05:00 offset reads, as text, four hours later than it is -- so a bound an
    hour BEFORE a note excluded it.

    The expected answer is derived from the instants, not from the strings: the
    bound precedes the note, therefore the note is in range, whatever the two
    representations look like side by side.
    """
    import datetime as dt

    store = Store(":memory:")
    store.create_note("alpha", "a note about bilinear transforms")
    created = store.db.execute("SELECT created_at FROM note").fetchone()[0]

    bound = (dt.datetime.fromisoformat(created) - dt.timedelta(hours=1)).astimezone(
        dt.timezone(dt.timedelta(hours=5))
    )
    assert bound.isoformat() > created, "this case does not discriminate; pick another offset"
    assert len(store.context("bilinear", since=bound.isoformat())) == 1


def test_since_is_validated_on_the_recency_listing_too():
    """An empty query is still a query with a bound on it."""
    store = Store(":memory:")
    store.create_note("alpha", "some body")
    with pytest.raises(SeshatError, match="ISO 8601"):
        store.context("", since="yesterday")


def test_a_naive_since_is_read_as_utc():
    """Every stored stamp is UTC, so a bound without an offset means UTC. The
    alternative -- local time -- would make results depend on the machine."""
    import datetime as dt

    store = Store(":memory:")
    store.create_note("alpha", "a note about bilinear transforms")
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(tzinfo=None)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(tzinfo=None)
    assert len(store.context("bilinear", since=past.isoformat())) == 1
    assert len(store.context("bilinear", since=future.isoformat())) == 0


# --------------------------------------- §6.3 writes do not wait on the embedder


def test_a_write_does_not_block_behind_a_query_side_embed():
    """worker.py states the invariant that follows from priority 1: writes must
    not block on embedding. It held on the write path -- embedding is off it
    entirely -- and was defeated transitively, because `context` embedded inside
    the store lock and `create_note` takes the same lock.

    The bound that matters is QUERY_TIMEOUT (10s), not the 2s used here.
    """
    import threading
    import time

    class Slow:
        model = "slow"
        dim = 4

        def __init__(self):
            self.entered = threading.Event()

        def embed(self, texts, timeout=0):
            self.entered.set()
            time.sleep(2.0)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedder = Slow()
    store = Store(":memory:", embedder=embedder)
    if not store.vector_loaded:
        pytest.skip("sqlite-vec not available")
    store.create_note("seed", "a seed note")
    EmbeddingWorker(store).drain()

    query = threading.Thread(target=lambda: store.context("seed"), daemon=True)
    query.start()
    assert embedder.entered.wait(5), "the embed never started"

    started = time.monotonic()
    store.create_note("write", "a write that must not wait")
    elapsed = time.monotonic() - started
    query.join(timeout=10)

    assert elapsed < 0.5, f"write blocked {elapsed:.2f}s behind an in-flight embed"
