"""The consistency checker. Spec §7.

Every check gets a positive case and is also required not to fire on a clean
store -- a checker that reports something on healthy data trains you to ignore
it, which is worse than not having it.
"""

import sqlite3

import pytest

from seshat.checker import Checker, format_report, reading_guide
from seshat.snapshots import Fetched, SnapshotStore, SnapshotWorker, snapshot_path_for
from seshat.store import Store


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "notes.db", snapshot_path_for(tmp_path / "notes.db")


@pytest.fixture
def built(paths):
    """A store the tests fill in, then check after closing."""
    notes, snaps = paths
    store = Store(notes)
    yield store
    store.close()


def check(paths, semantic=False, **kw):
    notes, snaps = paths
    checker = Checker(str(notes), snaps, **kw)
    try:
        return checker.run(semantic=semantic)
    finally:
        checker.close()


def kinds(findings):
    return sorted(f.check for f in findings)


# --------------------------------------------------------------- clean store


def test_a_healthy_store_produces_nothing(built, paths):
    a, _ = built.create_note("a finding", "body")
    b, _ = built.create_note("a revision", "different body", [{"id": a, "retained": 0.4}])
    built.create_note("unrelated", "another body")
    built.create_note("points at a head", f"see {b} for the current version")

    assert check(paths) == []


def test_empty_store_produces_nothing(built, paths):
    assert check(paths) == []


# ------------------------------------------------------------ §7.1 structural


def test_cycle_is_an_error(built, paths):
    """Unreachable given §3.4's write-time rejection, which is why it is worth
    checking -- this forces one in behind the store's back."""
    a, _ = built.create_note("a", "body a")
    b, _ = built.create_note("b", "body b", [{"id": a, "retained": 0.5}])
    built.db.execute(
        "INSERT INTO assessment(old_id, new_id, retained, asserted_at) VALUES (?,?,?,?)",
        (b, a, 0.5, "2026-01-01T00:00:00.000000+00:00"),
    )
    built.db.commit()

    found = check(paths)
    assert [f.check for f in found] == ["cycle"]
    assert found[0].severity == "error"
    assert set(found[0].notes) == {a, b}


def test_self_supersession_is_an_error(built, paths):
    a, _ = built.create_note("a", "body")
    built.db.execute(
        "INSERT INTO assessment(old_id, new_id, retained, asserted_at) VALUES (?,?,?,?)",
        (a, a, 1.0, "2026-01-01T00:00:00.000000+00:00"),
    )
    built.db.commit()

    found = [f for f in check(paths) if f.check == "self-supersession"]
    assert len(found) == 1 and found[0].severity == "error"


def test_noop_supersession_is_a_warning(built, paths):
    a, _ = built.create_note("same handle", "same body")
    built.create_note("same handle", "same body", [{"id": a, "retained": 1.0}])

    found = [f for f in check(paths) if f.check == "no-op supersession"]
    assert len(found) == 1 and found[0].severity == "warning"


def test_a_real_revision_is_not_a_noop(built, paths):
    a, _ = built.create_note("same handle", "original body")
    built.create_note("same handle", "revised body", [{"id": a, "retained": 1.0}])
    assert [f for f in check(paths) if f.check == "no-op supersession"] == []


def test_conflicting_fork(built, paths):
    """One descendant says the content survives, another says it does not."""
    a, _ = built.create_note("contested", "body")
    built.create_note("keeps it", "b", [{"id": a, "retained": 0.9}])
    built.create_note("rejects it", "c", [{"id": a, "retained": 0.1}])

    found = [f for f in check(paths) if f.check == "conflicting fork"]
    assert len(found) == 1
    assert found[0].severity == "review"
    assert found[0].notes == (a,)


def test_agreeing_fork_is_not_flagged(built, paths):
    a, _ = built.create_note("uncontested", "body")
    built.create_note("b", "b", [{"id": a, "retained": 0.9}])
    built.create_note("c", "c", [{"id": a, "retained": 1.0}])
    assert [f for f in check(paths) if f.check == "conflicting fork"] == []


def test_orphaned_retraction_is_the_follow_up_you_owe(built, paths):
    """§5.4/§7.1's highest-value check. B asserted A's content at 1.0; A is now
    retracted, so B is contaminated and nothing records it."""
    a, _ = built.create_note("original claim", "body")
    b, _ = built.create_note("builds on A", "addendum", [{"id": a, "retained": 1.0}])
    built.create_note("A was wrong", "retraction", [{"id": a, "retained": 0.0}])

    found = [f for f in check(paths) if f.check == "orphaned retraction"]
    assert len(found) == 1
    assert found[0].notes == (a, b)
    assert "has not been superseded" in found[0].message


def test_orphaned_retraction_clears_once_you_supersede_the_descendant(built, paths):
    a, _ = built.create_note("original claim", "body")
    b, _ = built.create_note("builds on A", "addendum", [{"id": a, "retained": 1.0}])
    built.create_note("A was wrong", "retraction", [{"id": a, "retained": 0.0}])
    built.create_note("and so B is too", "cleanup", [{"id": b, "retained": 0.0}])

    assert [f for f in check(paths) if f.check == "orphaned retraction"] == []


def test_dead_justification(built, paths):
    """A is in the pool only via B, and B is itself retracted -- specific to
    maximum (§5.2)."""
    a, _ = built.create_note("rests on B", "body")
    b, _ = built.create_note("B", "body b", [{"id": a, "retained": 1.0}])
    built.create_note("B was wrong", "retraction", [{"id": b, "retained": 0.0}])

    found = [f for f in check(paths) if f.check == "dead justification"]
    assert len(found) == 1 and found[0].severity == "review"
    assert a in found[0].notes and b in found[0].notes


def test_dead_justification_not_flagged_when_another_edge_holds(built, paths):
    a, _ = built.create_note("rests on two things", "body")
    b, _ = built.create_note("B", "body b", [{"id": a, "retained": 1.0}])
    built.create_note("C", "body c", [{"id": a, "retained": 1.0}])
    built.create_note("B was wrong", "retraction", [{"id": b, "retained": 0.0}])

    assert [f for f in check(paths) if f.check == "dead justification"] == []


def test_double_retraction(built, paths):
    a, _ = built.create_note("a", "body a")
    b, _ = built.create_note("a was wrong", "body b", [{"id": a, "retained": 0.0}])
    built.create_note("no, b was wrong", "body c", [{"id": b, "retained": 0.0}])

    found = [f for f in check(paths) if f.check == "double retraction"]
    assert len(found) == 1
    assert "judgement call" in found[0].message


def test_dangling_internal_link(built, paths):
    """A reference to a note id that does not exist: likely fabricated (§6.1)."""
    built.create_note("cites something imaginary", "as shown in zoo-zoo-zoo-zoo")

    found = [f for f in check(paths) if f.check == "dangling internal link"]
    assert len(found) == 1
    assert found[0].detail["target"] == "zoo-zoo-zoo-zoo"


# ------------------------------------------------------------- §7.1 snapshots


def test_missing_snapshot_is_an_error(paths):
    """Preservation failed silently -- the whole reason §6.6 exists."""
    notes, snap_path = paths
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    store.create_note("cites a page", "see https://example.com/never-captured")
    # Drop the queue entry without capturing: the fetch was lost.
    snaps.db.execute("DELETE FROM fetch_queue")
    snaps.db.commit()
    store.close()
    snaps.close()

    found = [f for f in check(paths) if f.check == "missing snapshot"]
    assert len(found) == 1 and found[0].severity == "error"


def test_a_queued_capture_is_not_yet_a_failure(paths):
    notes, snap_path = paths
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    store.create_note("cites a page", "see https://example.com/pending")
    store.close()
    snaps.close()

    assert [f for f in check(paths) if f.check == "missing snapshot"] == []


def test_thin_snapshot_is_flagged_as_time_sensitive(paths):
    notes, snap_path = paths
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    store.create_note("cites a page", "see https://example.com/paywalled")
    SnapshotWorker(
        snaps, fetcher=lambda t: Fetched(status="thin", http_status=200, body=b"x")
    ).run_once()
    store.close()
    snaps.close()

    found = [f for f in check(paths) if f.check == "thin snapshot"]
    assert len(found) == 1
    assert "TIME-SENSITIVE" in found[0].message


def test_snapshot_checks_are_skipped_without_a_snapshot_database(built, paths):
    """Capture disabled is a configuration, not a finding."""
    built.create_note("cites a page", "see https://example.com/x")
    notes, _ = paths
    checker = Checker(str(notes), None)
    try:
        assert [f for f in checker.run(semantic=False) if "snapshot" in f.check] == []
    finally:
        checker.close()


# ------------------------------------------------------------- §7.2 semantic


def test_description_fix_is_decidable(built, paths):
    """§7.2's one narrow exception -- and the case §9.3 wants to know about."""
    body = "the decay constant is tau = -1/ln(1 - 1/p), roughly p minus one half"
    a, _ = built.create_note("notes on filters", body)
    built.create_note("Monopole decay constant is tau = -1/ln(1-1/p)", body,
                      [{"id": a, "retained": 1.0}])

    found = [f for f in check(paths, semantic=True) if f.check == "description fix"]
    assert len(found) == 1
    assert found[0].detail["was"] == "notes on filters"
    assert found[0].severity == "info"


def test_a_substantive_revision_is_not_a_description_fix(built, paths):
    a, _ = built.create_note("first handle", "the original body of the note")
    built.create_note("second handle", "an entirely different body saying other things",
                      [{"id": a, "retained": 0.5}])
    assert [f for f in check(paths, semantic=True) if f.check == "description fix"] == []


def test_suggested_links_need_vectors_and_degrade_quietly(built, paths):
    """No embeddings means no candidates -- not an error."""
    built.create_note("a", "body a")
    built.create_note("b", "body b")
    assert [f for f in check(paths, semantic=True) if f.check == "suggested link"] == []


# --------------------------------------------------------- never mutates


def fingerprint(path) -> list:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    state = []
    for table in sorted(tables):
        try:
            state.append((table, db.execute(f"SELECT * FROM {table}").fetchall()))
        except sqlite3.OperationalError:
            pass  # virtual table shadow structures
    db.close()
    return state


def test_the_checker_never_mutates(built, paths):
    """§7: it never mutates, never auto-creates edges, never auto-fixes."""
    a, _ = built.create_note("contested", "body")
    built.create_note("keeps it", "b", [{"id": a, "retained": 0.9}])
    built.create_note("rejects it", "c", [{"id": a, "retained": 0.1}])
    built.create_note("dangling", "see zoo-zoo-zoo-zoo")
    notes, _ = paths
    built.db.commit()

    before = fingerprint(notes)
    assert check(paths, semantic=True), "the store must actually have findings"
    assert fingerprint(notes) == before


def test_the_checker_opens_the_store_read_only(built, paths):
    """Enforced by SQLite, not promised in a docstring."""
    notes, _ = paths
    checker = Checker(str(notes), None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            checker.db.execute("DELETE FROM note")
    finally:
        checker.close()


def test_checking_a_missing_store_is_an_error_not_a_new_store(tmp_path):
    """mode=ro will not create a file, which is the point."""
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        Checker(str(tmp_path / "nonexistent.db"), None)


# ------------------------------------------------------------- the report


def test_report_names_where_to_start_reading(built, paths):
    """§7.3: checker output doubles as a reading guide."""
    a, _ = built.create_note("the troubled one", "body")
    built.create_note("keeps it", "b", [{"id": a, "retained": 0.9}])
    built.create_note("rejects it", "c", [{"id": a, "retained": 0.1}])
    built.create_note("retracts it", "d", [{"id": a, "retained": 0.0}])

    findings = check(paths)
    descs = dict(built.db.execute("SELECT id, desc FROM note"))
    guide = reading_guide(findings, descs)
    assert guide[0][0] == a, "the most-implicated note comes first"

    report = format_report(findings, descs, total_notes=4)
    assert "WHERE TO START READING" in report
    assert a in report


def test_a_clean_report_does_not_claim_consistency(built, paths):
    built.create_note("a", "body")
    report = format_report(check(paths), {}, total_notes=1)
    assert "No findings" in report
    assert "not proof" in report


def test_suggested_links_finds_similar_notes_with_no_path_between_them(tmp_path):
    """§7.2, candidate generation. Two notes that look alike and have no
    supersession edge between them are a candidate missing link -- and nothing
    more than a candidate."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_vectors import FakeEmbedder
    from seshat.worker import EmbeddingWorker

    embedder = FakeEmbedder()
    notes = tmp_path / "n.db"
    store = Store(notes, embedder=embedder)
    if not store.vector_loaded:
        pytest.skip("sqlite-vec unavailable")

    # Written twice, months apart, with no edge between them: exactly the
    # duplicate that supersession exists to fold together and that nothing in
    # the store would otherwise surface.
    twin_a, _ = store.create_note("prewarping the bilinear transform", "tan of omega T over two")
    twin_b, _ = store.create_note("prewarping the bilinear transform", "tan of omega T over two")
    linked_a, _ = store.create_note("gearbox backlash deadband", "feed forward")
    linked_b, _ = store.create_note("gearbox backlash deadband", "feed forward",
                                    [{"id": linked_a, "retained": 1.0}])
    EmbeddingWorker(store).drain()
    store.close()

    checker = Checker(str(notes), None, similarity=0.99)
    try:
        found = [f for f in checker.run(semantic=True) if f.check == "suggested link"]
    finally:
        checker.close()

    pairs = {frozenset(f.notes) for f in found}
    assert frozenset({twin_a, twin_b}) in pairs, "identical notes with no edge are a candidate"
    assert frozenset({linked_a, linked_b}) not in pairs, "an existing path is not a candidate"
    assert all(f.severity == "info" for f in found), "candidates are not defects"


def test_an_unmet_expectation_is_a_review_candidate_not_an_error(paths):
    """§6.9: like §7.2's suggested links, candidate generation for review. A
    statistical table may contain none of the expected words in prose while
    being a perfect capture, so this can never be a verdict."""
    notes, snap_path = paths
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    store.create_note("state demographics",
                      "see [texas population data](https://example.gov/tx) for figures")
    SnapshotWorker(snaps, fetcher=lambda t: Fetched(
        status="ok", http_status=200, content_type="text/html",
        body=b"<p>Subscribe to continue reading. Sign in to your account today. "
             b"Members get unlimited access to everything we publish, cancel anytime.</p>",
    )).run_once()
    store.close()
    snaps.close()

    found = [f for f in check(paths) if f.check == "expectation not met"]
    assert len(found) == 1
    assert found[0].severity == "review", "never an error"
    assert "candidate, not a verdict" in found[0].message


def test_a_met_expectation_is_not_flagged(paths):
    notes, snap_path = paths
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    store.create_note("state demographics",
                      "see [texas population data](https://example.gov/tx) for figures")
    SnapshotWorker(snaps, fetcher=lambda t: Fetched(
        status="ok", http_status=200, content_type="text/html",
        body=b"<p>Texas population data for the last decade, by county.</p>",
    )).run_once()
    store.close()
    snaps.close()

    assert [f for f in check(paths) if f.check == "expectation not met"] == []


# --------------------------------------------- §7.2 similarity is a real cosine


class _FixedEmbedder:
    """Returns a chosen vector per note, so the expected cosine is known in
    closed form before any code under test runs."""

    model = "fixed"
    dim = 4

    def __init__(self, vectors):
        self.vectors = vectors
        self.seen = 0

    def embed(self, texts, timeout=0):
        out = []
        for _ in texts:
            out.append(self.vectors[min(self.seen, len(self.vectors) - 1)])
            self.seen += 1
        return out


def _embedded_store(tmp_path, vectors):
    from seshat.worker import EmbeddingWorker

    path = tmp_path / "n.db"
    store = Store(path, embedder=_FixedEmbedder(vectors))
    if not store.vector_loaded:
        pytest.skip("sqlite-vec not available")
    for index in range(len(vectors)):
        store.create_note(f"note {index}", f"body of note {index}")
    EmbeddingWorker(store).drain()
    store.db.commit()
    store.close()
    return path


def test_similarity_is_a_cosine_not_a_dot_product(tmp_path):
    """Magnitude must not reach the score. Two parallel vectors of magnitude 2
    have cosine 1.0; their dot product is 4.0, which is not a similarity and is
    not comparable to `--similarity` at all.

    The expected value is derived by hand from the definition, not from the
    implementation: cos = 1 for parallel vectors, whatever their length.
    """
    path = _embedded_store(tmp_path, [[2.0, 0.0, 0.0, 0.0], [2.0, 0.0, 0.0, 0.0]])
    checker = Checker(str(path), similarity=0.5)
    try:
        findings = checker.check_suggested_links()
    finally:
        checker.close()
    assert len(findings) == 1
    assert findings[0].detail["similarity"] == pytest.approx(1.0, abs=1e-6)


def test_similarity_agrees_with_sqlite_vec(tmp_path):
    """Two independent paths to the same number: this checker's Python, and
    `vec_distance_cosine` in sqlite-vec's C. What is asserted is the residual.

    Independence is the point. Recomputing the dot product in the test the same
    way the checker does would reproduce a missing normalisation in green.
    """
    import math

    a = [1.0, 2.0, 3.0, 4.0]
    b = [4.0, 1.0, 0.5, 2.0]
    path = _embedded_store(tmp_path, [a, b])

    checker = Checker(str(path), similarity=-1.0)
    try:
        findings = checker.check_suggested_links()
        ours = findings[0].detail["similarity"]

        from seshat import vectors as vectors_module

        assert vectors_module.load_extension(checker.db)
        theirs = checker.db.execute(
            """SELECT vec_distance_cosine(
                   (SELECT embedding FROM note_vec LIMIT 1),
                   (SELECT embedding FROM note_vec LIMIT 1 OFFSET 1)) AS d"""
        ).fetchone()["d"]
    finally:
        checker.close()

    # And a third, closed-form: cos = a.b / (|a||b|), computed from the inputs
    # rather than from anything either path stored.
    dot = sum(x * y for x, y in zip(a, b))
    by_hand = dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    assert abs(ours - (1.0 - theirs)) < 1e-5, "checker disagrees with sqlite-vec"
    assert abs(ours - by_hand) < 1e-5, "checker disagrees with the definition"


def test_a_zero_vector_is_excluded_rather_than_divided_by(tmp_path):
    """No direction, so no angle to anything. The alternative is a ZeroDivision
    in a maintenance command, or treating it as orthogonal to everything, which
    asserts something false."""
    path = _embedded_store(tmp_path, [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]])
    checker = Checker(str(path), similarity=-1.0)
    try:
        assert checker.check_suggested_links() == []
    finally:
        checker.close()
