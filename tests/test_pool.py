"""Pool membership and `retained` combination. Spec §5.1, §5.2, §5.4, §5.5.

This is the file that matters most. The pool query is the one thing in the
system that can be silently wrong while every call still appears to work.
"""

import random

import pytest

import reference
from seshat.store import Store


def db_pool(store: Store) -> set[str]:
    rows = store.db.execute(
        "SELECT id FROM pool_retained WHERE retained >= ?", (store.theta,)
    ).fetchall()
    return {r["id"] for r in rows}


def all_notes(store: Store) -> set[str]:
    return {r[0] for r in store.db.execute("SELECT id FROM note")}


def live_edges(store: Store) -> dict[tuple[str, str], float]:
    return {
        (r["old_id"], r["new_id"]): r["retained"]
        for r in store.db.execute("SELECT old_id, new_id, retained FROM supersession")
    }


def test_never_superseded_notes_are_in_the_pool(store, mk):
    """The LEFT JOIN trap. An inner join drops every note with no outgoing edge
    -- the majority of any real store -- and `context` still looks like it works
    because it keeps returning the revised ones."""
    lonely = [mk(f"note {i}") for i in range(5)]
    assert db_pool(store) == set(lonely)

    # And one revised note must not change that for the others.
    new, _ = store.create_note("revision", "body", [{"id": lonely[0], "retained": 0.0}])
    assert db_pool(store) == set(lonely[1:]) | {new}


def test_pool_matches_independent_computation(store):
    """Cross-check the SQL against a plain-Python reading of §5.1."""
    notes = [store.create_note(f"n{i}", f"body {i}")[0] for i in range(6)]
    spec_edges = [(0, 3, 1.0), (1, 3, 0.4), (2, 4, 0.9), (1, 5, 0.2), (4, 5, 0.0)]
    for old, new, retained in spec_edges:
        store.add_assessment(notes[old], notes[new], retained)

    edges = {(notes[o], notes[n]): r for o, n, r in spec_edges}
    expected = reference.pool(notes, edges, store.theta)
    assert db_pool(store) == expected
    # Sanity-check the checker: the expectation must not be vacuous.
    assert 0 < len(expected) < len(notes)


def test_maximum_not_minimum_across_paths(store, mk):
    """§5.2. A retraction alongside a live additive edge must not drop the old
    note: B asserted A's content, so A is still load-bearing (§5.4)."""
    a = mk("original finding")
    b, _ = store.create_note("adds to A", "addendum", [{"id": a, "retained": 1.0}])
    c, _ = store.create_note("A was wrong", "retraction", [{"id": a, "retained": 0.0}])

    assert db_pool(store) >= {a, b, c}, "max must keep A; min would have dropped it"
    retained = store.db.execute(
        "SELECT retained FROM pool_retained WHERE id = ?", (a,)
    ).fetchone()[0]
    assert retained == 1.0


def test_pool_is_independent_of_edge_insertion_order(store, mk):
    """Associativity, commutativity and idempotence of max, in one property.

    Those three are exactly what §5.2 leans on, and in an append-only store
    where edges cannot be retracted they are the difference between a score and
    a race condition.
    """
    labels = list("abcdef")
    spec_edges = [
        ("a", "d", 1.0), ("b", "d", 0.3), ("c", "e", 0.85),
        ("b", "f", 0.95), ("d", "f", 0.1), ("a", "e", 0.0),
    ]

    results = []
    for seed in range(6):
        s = Store(":memory:")
        ids = {label: s.create_note(label, f"body {label}")[0] for label in labels}
        order = list(spec_edges)
        random.Random(seed).shuffle(order)
        for old, new, retained in order:
            s.add_assessment(ids[old], ids[new], retained)
        # Re-record one edge verbatim: idempotence, not a new assessment outcome.
        s.add_assessment(ids["a"], ids["d"], 1.0)
        results.append({label for label, i in ids.items() if i in db_pool(s)})
        s.close()

    assert all(r == results[0] for r in results), results
    expected = reference.pool(
        labels, {(o, n): r for o, n, r in spec_edges}, Store(":memory:").theta
    )
    assert results[0] == expected


def test_reassessment_can_drop_a_note_from_the_pool(store, mk):
    """§5.5: monotone under edge *addition*, but re-assessment may lower a score
    -- deliberately, and with the old value still on the record."""
    a = mk("claim")
    b, _ = store.create_note("refines A", "body", [{"id": a, "retained": 1.0}])
    assert a in db_pool(store)

    result = store.add_assessment(a, b, 0.1, why="on re-reading, B kept almost nothing of A")
    assert result["reassessment"] is True
    assert result["previous_retained"] == 1.0
    assert a not in db_pool(store)

    history = store.db.execute(
        "SELECT retained FROM assessment WHERE old_id=? AND new_id=? ORDER BY asserted_at", (a, b)
    ).fetchall()
    assert [h[0] for h in history] == [1.0, 0.1], "history must survive re-assessment"


def test_latest_wins_matches_independent_resolution(store, mk):
    a = mk("claim")
    b, _ = store.create_note("revision", "body", [{"id": a, "retained": 0.9}])
    for value in (0.4, 0.75, 0.2):
        store.add_assessment(a, b, value)

    rows = store.db.execute(
        "SELECT old_id, new_id, retained, asserted_at FROM assessment"
    ).fetchall()
    expected = reference.latest_edges([tuple(r) for r in rows])
    assert live_edges(store) == expected
    assert expected[(a, b)] == 0.2


def test_diamond_resolves_without_traversal(store, mk):
    """§5.1/§5.2: whether D reaches A via B or via C is irrelevant to A."""
    a = mk("root")
    b, _ = store.create_note("left", "l", [{"id": a, "retained": 0.5}])
    c, _ = store.create_note("right", "r", [{"id": a, "retained": 0.5}])
    d, _ = store.create_note("merge", "m", [{"id": b, "retained": 1.0}, {"id": c, "retained": 1.0}])

    retained = dict(store.db.execute("SELECT id, retained FROM pool_retained"))
    assert retained[a] == 0.5, "max over two 0.5 direct edges, not a composed path value"
    assert retained[b] == retained[c] == 1.0
    assert retained[d] == 1.0


@pytest.mark.parametrize("theta", [0.0, 0.5, 0.8, 1.0])
def test_theta_is_a_threshold_on_direct_max(tmp_path, theta):
    s = Store(tmp_path / f"t{theta}.db", theta=theta)
    a, _ = s.create_note("a", "body a")
    b, _ = s.create_note("b", "body b", [{"id": a, "retained": 0.6}])
    expected = reference.pool([a, b], {(a, b): 0.6}, theta)
    assert db_pool(s) == expected
    s.close()
