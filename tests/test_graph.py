"""DAG structure: heads, forks, merges, cycles, truncation. Spec §2, §3.3-§3.5."""

import pytest

import reference
from seshat.store import CycleError, SeshatError


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
