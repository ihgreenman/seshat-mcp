"""DAG structure: heads, forks, merges, cycles, truncation. Spec §2, §3.3-§3.5."""

import pytest

import reference
from seshat.store import CycleError, SeshatError, Store


def edge_pairs(store):
    return [(r["old_id"], r["new_id"]) for r in store.db.execute("SELECT old_id, new_id FROM supersession")]


def test_heads_is_a_set_not_a_single_note(store, mk):
    """§2: superseding something already superseded forks the graph, and that
    happens by accident. Nothing may assume uniqueness."""
    a = mk("original")
    b, _ = store.create_note("first revision", "b", [{"id": a, "retained": 0.5}])
    c, _ = store.create_note("second revision, forgot about B", "c", [{"id": a, "retained": 0.5}])

    assert set(store.heads(a)) == {b, c}
    assert set(store.heads(a)) == reference.heads(a, edge_pairs(store))


def test_a_note_with_no_successors_is_its_own_head(store, mk):
    a = mk("solo")
    assert store.heads(a) == [a]


def test_heads_walks_the_whole_chain(store, mk):
    """Without this, walking A<-B<-C from A costs two extra calls and the
    likely outcome is the reader giving up and using the stale note (§3.3)."""
    a = mk("a")
    b, _ = store.create_note("b", "b", [{"id": a, "retained": 0.5}])
    c, _ = store.create_note("c", "c", [{"id": b, "retained": 0.5}])
    assert store.heads(a) == [c]
    assert set(store.heads(a)) == reference.heads(a, edge_pairs(store))


def test_merge_note_supersedes_many(store, mk):
    """The merge direction is the one that legitimately grows -- consolidating
    scattered notes is the recommended response to §8."""
    olds = [mk(f"scattered {i}") for i in range(4)]
    merged, _ = store.create_note(
        "consolidated", "body", [{"id": o, "retained": 1.0} for o in olds]
    )
    record = store.read(merged)
    assert record.supersedes_total == 4
    assert {e["id"] for e in record.supersedes} == set(olds)
    for old in olds:
        assert store.heads(old) == [merged]


def test_truncation_reports_the_pre_truncation_total(store, mk):
    """§3.3: a reader shown five of twelve concludes the note absorbed five."""
    olds = [mk(f"old {i}") for i in range(12)]
    merged, _ = store.create_note(
        "merge", "body", [{"id": o, "retained": 1.0} for o in olds]
    )
    record = store.read(merged, edge_limit=5)
    assert len(record.supersedes) == 5
    assert record.supersedes_total == 12


def test_truncation_orders_by_retained_descending(store, mk):
    olds = [mk(f"old {i}") for i in range(4)]
    scores = [0.1, 1.0, 0.5, 0.9]
    merged, _ = store.create_note(
        "merge", "body", [{"id": o, "retained": s} for o, s in zip(olds, scores)]
    )
    record = store.read(merged, edge_limit=2)
    assert [e["retained"] for e in record.supersedes] == [1.0, 0.9]


def test_superseded_by_is_always_returned(store, mk):
    """Ids leak forward in a conversation; a body-only read makes confident use
    of a retracted note the default behaviour (§3.3)."""
    a = mk("wrong thing")
    b, _ = store.create_note("correction", "b", [{"id": a, "retained": 0.0}])
    record = store.read(a)
    assert record.superseded_by == [{"id": b, "retained": 0.0, "rationale": None}]
    assert record.superseded_by_total == 1
    assert record.heads == [b]


def test_retroactive_cycle_is_rejected(store, mk):
    """§3.4. Creation-time supersession cannot cycle; this one can, and a cycle
    makes head resolution non-terminating."""
    a = mk("a")
    b, _ = store.create_note("b", "b", [{"id": a, "retained": 0.5}])
    c, _ = store.create_note("c", "c", [{"id": b, "retained": 0.5}])

    with pytest.raises(CycleError):
        store.add_assessment(c, a, 0.5)
    with pytest.raises(CycleError):
        store.add_assessment(b, a, 0.5)
    # Rejected at write time: nothing recorded.
    assert store.heads(a) == [c]


def test_self_supersession_is_rejected(store, mk):
    a = mk("a")
    with pytest.raises(CycleError):
        store.add_assessment(a, a, 1.0)


def test_chain_returns_raw_edge_scores(store, mk):
    """§5.3: two hops at 0.5 bound the survival of A in C only to [0, 0.5].
    Any aggregate would be false precision, and a reader would believe it."""
    a = mk("a")
    b, _ = store.create_note("b", "b", [{"id": a, "retained": 0.5}])
    c, _ = store.create_note("c", "c", [{"id": b, "retained": 0.5}])

    result = store.chain(b)
    assert {n["id"] for n in result["nodes"]} == {a, b, c}, "ancestors and descendants"
    assert sorted((e["old_id"], e["new_id"], e["retained"]) for e in result["edges"]) == sorted(
        [(a, b, 0.5), (b, c, 0.5)]
    )
    assert all("retained" in e for e in result["edges"])
    assert not any("composed" in e or "aggregate" in e for e in result["edges"])


def test_chain_of_an_isolated_note(store, mk):
    a = mk("alone")
    result = store.chain(a)
    assert [n["id"] for n in result["nodes"]] == [a]
    assert result["edges"] == []


def test_retained_out_of_range_is_rejected(store, mk):
    a = mk("a")
    b = mk("b")
    for bad in (-0.1, 1.5):
        with pytest.raises(SeshatError):
            store.add_assessment(a, b, bad)


def test_desc_and_text_are_required(store):
    with pytest.raises(SeshatError):
        store.create_note("", "body")
    with pytest.raises(SeshatError):
        store.create_note("desc", "   ")


def test_creation_edges_are_atomic_with_the_note(store, mk):
    """§3.1: a separate second call is a call that gets forgotten, leaving two
    contradictory notes with no edge between them."""
    a = mk("a")
    before = store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
    with pytest.raises(SeshatError):
        store.create_note("b", "body", [{"id": a, "retained": 42.0}])
    assert store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0] == before


def test_an_unknown_edge_key_is_refused_rather_than_ignored(store, mk):
    """Reported from real use: `rationale` -- the *column* name in §5.5 -- was
    passed where the argument is `why`, accepted, and dropped. The caller was
    told the edge was written and believed the reasoning went with it.

    Ignoring an unrecognised key is indistinguishable from honouring it at the
    call site, which makes it a priority-2 failure: not a lost feature, a false
    belief about what the store holds.
    """
    a = mk("a")
    before = store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
    with pytest.raises(SeshatError) as exc:
        store.create_note("b", "body", [{"id": a, "retained": 0.5, "rationale": "why not"}])
    assert "why" in str(exc.value), "say what the argument is actually called"
    assert store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0] == before


def test_a_missing_required_edge_key_names_itself(store, mk):
    """Previously a bare KeyError, which `reported` does not translate -- the
    caller saw a generic crash instead of the one word that was wrong."""
    a = mk("a")
    with pytest.raises(SeshatError) as exc:
        store.create_note("b", "body", [{"id": a}])
    assert "retained" in str(exc.value)
    with pytest.raises(SeshatError):
        store.create_note("b", "body", [{"retained": 0.5}])
    with pytest.raises(SeshatError):
        store.create_note("b", "body", ["not-a-dict"])


def test_a_good_key_still_reaches_the_rationale_column(store, mk):
    """The rejection must not be so eager that it breaks the working path: the
    argument `why` lands in the column `rationale`, and that rename is the
    whole source of the confusion."""
    a = mk("a")
    b, _ = store.create_note("b", "body", [{"id": a, "retained": 0.5, "why": "sign error"}])
    stored = store.db.execute(
        "SELECT rationale FROM supersession WHERE old_id = ? AND new_id = ?", (a, b)
    ).fetchone()[0]
    assert stored == "sign error"


def test_a_partly_bad_edge_list_writes_nothing(store, mk):
    """Validation runs over every edge before the first one is resolved. A note
    that exists carrying half its requested edges is the same lie one table
    over -- and harder to notice, since the note itself looks fine."""
    a, b = mk("a"), mk("b")
    before = store.db.execute("SELECT COUNT(*) FROM supersession").fetchone()[0]
    with pytest.raises(SeshatError):
        store.create_note("c", "body", [
            {"id": a, "retained": 0.5},
            {"id": b, "retained": 0.5, "reason": "typo'd key"},
        ])
    assert store.db.execute("SELECT COUNT(*) FROM supersession").fetchone()[0] == before


def test_a_non_numeric_retained_reports_what_is_wrong(tmp_path):
    """`float("high")` raises ValueError and `float(None)` raises TypeError, and
    neither is a SeshatError -- so both walked past `reported` and reached the
    model as "error executing tool", which says only to give up. The message
    that names the mistake is the whole point of that decorator."""
    store = Store(":memory:")
    first, _ = store.create_note("first", "body")

    for value in ("high", None, [0.5], {}):
        with pytest.raises(SeshatError, match="retained"):
            store.create_note("n", "t", [{"id": first, "retained": value}])

    assert store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0] == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_retained_is_refused(value):
    """NaN passes `0.0 <= x <= 1.0` by being false on both sides of nothing,
    and would sit in a REAL column poisoning every MAX() over it."""
    store = Store(":memory:")
    first, _ = store.create_note("first", "body")
    with pytest.raises(SeshatError, match="retained"):
        store.create_note("n", "t", [{"id": first, "retained": value}])


def test_an_integrity_error_that_is_not_a_collision_is_not_retried():
    """The timestamp loop exists for one thing: the same edge re-assessed twice
    inside one microsecond. Any other IntegrityError is not fixed by moving the
    timestamp, so retrying spins forever on a write that will never succeed.

    The failure is injected through a forwarding proxy rather than a patched
    method, because sqlite3.Connection.execute is read-only -- and a real
    non-collision IntegrityError cannot be provoked here: `_check_retained`
    catches the CHECK case before the insert and `resolve` guarantees both
    foreign keys exist.
    """
    import sqlite3

    class Interposed:
        """Forwards to the real connection, failing one statement."""

        def __init__(self, real):
            self._real = real
            self.attempts = 0

        def execute(self, sql, *args):
            if "INSERT INTO assessment" in sql:
                self.attempts += 1
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
            return self._real.execute(sql, *args)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = Store(":memory:")
    a, _ = store.create_note("a", "body a")
    b, _ = store.create_note("b", "body b")

    proxy = Interposed(store.db)
    store.db = proxy
    with pytest.raises(sqlite3.IntegrityError):
        store.add_assessment(a, b, 0.5)
    assert proxy.attempts == 1, "a non-collision failure must not be retried at all"


def test_a_real_timestamp_collision_is_still_absorbed():
    """The loop must keep doing its job: two assessments of one edge inside a
    microsecond are two facts, and neither may be lost."""
    store = Store(":memory:")
    a, _ = store.create_note("a", "body a")
    b, _ = store.create_note("b", "body b")

    import seshat.store as store_module

    fixed = store_module.now()
    original = store_module.now
    store_module.now = lambda: fixed
    try:
        store.add_assessment(a, b, 0.5, "first")
        store.add_assessment(a, b, 0.7, "second")
    finally:
        store_module.now = original

    rows = store.db.execute(
        "SELECT retained, rationale FROM assessment ORDER BY asserted_at"
    ).fetchall()
    assert [(r["retained"], r["rationale"]) for r in rows] == [(0.5, "first"), (0.7, "second")]
