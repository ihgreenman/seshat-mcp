"""Snapshot capture. Spec §6.6, §3.6.

Increment-one form: raw bytes plus a hash, no extraction. The deadline is real
-- a note written before a fetcher exists has permanently unrecoverable links --
so capture ships before extraction, and these tests are mostly about capture
happening at all and never being lost.

No test here touches the network. The fetcher is injected.
"""

import hashlib

import pytest

from seshat.snapshots import Fetched, SnapshotStore, SnapshotWorker, snapshot_path_for
from seshat.store import Store


@pytest.fixture
def snaps(tmp_path):
    s = SnapshotStore(tmp_path / "snaps.db")
    yield s
    s.close()


@pytest.fixture
def linked(tmp_path, snaps):
    store = Store(tmp_path / "notes.db", snapshots=snaps)
    yield store
    store.close()


def ok(body=b"<html><title>A page</title>body text</html>", ctype="text/html"):
    return lambda target: Fetched(status="ok", http_status=200, content_type=ctype, body=body)


def test_writing_a_note_queues_a_capture_without_blocking(linked):
    """§6.3/§6.6: note() never blocks on a fetch. The queue row is the whole
    contract at write time."""
    note_id, _ = linked.create_note(
        "nomic needs task prefixes", "per https://ollama.com/library/nomic-embed-text"
    )
    assert linked.snapshots.pending() == [(note_id, "https://ollama.com/library/nomic-embed-text")]
    # Nothing captured yet, and the note is fully usable regardless.
    assert linked.read(note_id).desc == "nomic needs task prefixes"


def test_only_urls_are_queued(linked, mk):
    """Note ids and content hashes need no fetching."""
    other = mk("another note")
    note_id, _ = linked.create_note(
        "mixed references",
        f"see {other} and artifact {'a' * 64} and https://example.com/only-this",
    )
    assert [t for _, t in linked.snapshots.pending()] == ["https://example.com/only-this"]


def test_capture_stores_bytes_and_a_hash_of_the_complete_body(linked):
    body = b"<html>the page as it stood</html>"
    note_id, _ = linked.create_note("a claim", "source: https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok(body)).run_once()

    sources = linked.snapshots.sources_for(note_id)
    assert len(sources) == 1
    source = sources[0]
    assert source["status"] == "ok"
    assert source["body_hash"] == hashlib.sha256(body).hexdigest()
    assert source["body_length"] == len(body)
    assert source["extracted"] is False, "bytes held, nothing parsed yet"
    assert source["extraction"] is None


def test_queue_is_drained_on_success(linked):
    linked.create_note("a claim", "source: https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()
    assert linked.snapshots.pending() == []


def test_a_dead_target_records_gone_and_is_not_retried(linked):
    """§6.6: a target already dead at capture is information, not an error."""
    note_id, _ = linked.create_note("a claim", "source: https://example.com/dead")
    worker = SnapshotWorker(
        linked.snapshots, fetcher=lambda t: Fetched(status="gone", http_status=404, error="HTTP 404")
    )
    worker.run_once()

    assert linked.snapshots.pending() == [], "not retried"
    assert linked.snapshots.sources_for(note_id)[0]["status"] == "gone"


def test_transient_failure_stays_queued_then_gives_up(linked):
    """Retryable, but not forever: a permanently failing row must eventually be
    recorded, because a queue that never empties hides the failure."""
    note_id, _ = linked.create_note("a claim", "source: https://example.com/flaky")
    worker = SnapshotWorker(
        linked.snapshots,
        fetcher=lambda t: Fetched(status="unreachable", error="timed out"),
        max_attempts=3,
    )
    worker.run_once()
    assert linked.snapshots.pending(), "first failure is retryable"

    worker.run_once()
    worker.run_once()
    assert linked.snapshots.pending() == []
    assert linked.snapshots.sources_for(note_id)[0]["status"] == "unreachable"


def test_a_broken_fetcher_cannot_kill_the_queue(linked):
    def explode(target):
        raise RuntimeError("fetcher is on fire")

    linked.create_note("a claim", "source: https://example.com/a")
    worker = SnapshotWorker(linked.snapshots, fetcher=explode, max_attempts=1)
    worker.run_once()
    assert linked.snapshots.pending() == []


def test_snapshots_are_immutable_once_taken(linked):
    """Note and snapshot seal together (§6.6). A re-queued capture must not
    overwrite an earlier, better witness."""
    note_id, _ = linked.create_note("a claim", "source: https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"original content")).run_once()
    first = linked.snapshots.sources_for(note_id)[0]

    linked.snapshots.enqueue(note_id, ["https://example.com/a"])
    SnapshotWorker(linked.snapshots, fetcher=ok(b"the page changed later")).run_once()
    second = linked.snapshots.sources_for(note_id)[0]

    assert second["body_hash"] == first["body_hash"]
    assert second["captured_at"] == first["captured_at"]


def test_two_notes_citing_one_target_get_two_witnesses(linked):
    """Per-note, not per-target: comparing the two IS the drift test (§6.6)."""
    first, _ = linked.create_note("earlier reading", "https://example.com/shared")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"as it stood in spring")).run_once()
    second, _ = linked.create_note("later reading", "https://example.com/shared")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"as it stood in autumn")).run_once()

    a = linked.snapshots.sources_for(first)[0]
    b = linked.snapshots.sources_for(second)[0]
    assert a["target"] == b["target"]
    assert a["body_hash"] != b["body_hash"], "drift is a query over your own history"


def test_superseding_a_note_leaves_its_witness_intact(linked):
    """The old belief was formed against the old page."""
    first, _ = linked.create_note("original claim", "https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()
    linked.create_note("revised claim", "no link here", [{"id": first, "retained": 0.2}])
    assert len(linked.snapshots.sources_for(first)) == 1


def test_a_failing_snapshot_store_cannot_fail_a_write(tmp_path, snaps):
    """Priority 1 outranks preservation: degrade to 'no witness', never to
    'the note was not written'."""
    store = Store(tmp_path / "notes.db", snapshots=snaps)
    snaps.close()  # the snapshot database is now unusable

    note_id, _ = store.create_note("a claim", "source: https://example.com/a")
    assert store.read(note_id).desc == "a claim"
    store.close()


def test_sources_are_opt_in(linked):
    note_id, _ = linked.create_note("a claim", "source: https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()

    assert linked.read(note_id).sources is None, "preserved text would wreck every return"
    assert len(linked.read(note_id, with_sources=True).sources) == 1


def test_histogram_counts_permanent_failures_not_just_backlog(linked):
    """§3.6: a bare counter reads zero while the data is missing, because a
    permanently failed fetch has already left the queue."""
    linked.create_note("a", "https://example.com/ok")
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()
    linked.create_note("b", "https://example.com/dead")
    SnapshotWorker(
        linked.snapshots, fetcher=lambda t: Fetched(status="gone", http_status=410)
    ).run_once()
    linked.create_note("c", "https://example.com/never-fetched")

    histogram = linked.snapshots.histogram()
    assert histogram["ok"] == 1
    assert histogram["gone"] == 1
    assert histogram["pending"] == 1
    assert histogram["thin"] == 0, "no extractor exists yet, so nothing can be thin"
    assert histogram["unextracted"] == 1, "bytes held but unparsed must not read as done"


def test_queue_is_durable_across_a_restart(tmp_path):
    """A capture that can never be taken again must not be lost to a crash
    between writing the note and fetching."""
    notes, snap_path = tmp_path / "n.db", tmp_path / "s.db"
    snaps = SnapshotStore(snap_path)
    store = Store(notes, snapshots=snaps)
    note_id, _ = store.create_note("a claim", "https://example.com/a")
    store.close()
    snaps.close()

    reopened = SnapshotStore(snap_path)
    assert reopened.pending() == [(note_id, "https://example.com/a")]
    reopened.close()


def test_snapshot_path_sits_beside_the_notes_store():
    assert snapshot_path_for("/x/notes.db") == "/x/notes.db.snapshots.db"


# ------------------------------------------------------------------ the hazard


def test_rebuilding_links_cannot_touch_snapshots(linked):
    """THE trap in the whole design (§6.6): `DELETE FROM link` is a safe
    rebuild, `DELETE FROM snapshot` is unrecoverable loss, and the two share a
    primary key. Separate database files are what make the safe one safe."""
    note_id, _ = linked.create_note("a claim", "source: https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()
    assert linked.snapshots.sources_for(note_id)

    linked.reindex_links()

    assert linked.snapshots.sources_for(note_id), "the witness must survive a rebuild"
    assert linked.read(note_id).links, "and the derived index must come back"


def test_the_notes_database_has_no_snapshot_table(linked):
    """Structural, not disciplinary: the rebuild cannot reach `snapshot`
    because that table does not exist in the database it runs against."""
    tables = {
        r[0] for r in linked.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "link" in tables
    assert "snapshot" not in tables and "raw" not in tables


def test_the_snapshot_database_has_no_link_table(snaps):
    tables = {r[0] for r in snaps.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshot", "raw", "fetch_queue"} <= tables
    assert "link" not in tables and "note" not in tables


def test_capture_can_be_turned_off_entirely(tmp_path):
    """Writing a note fetches URLs from its text. That is the preservation
    model (§6.6), and it is also outbound network traffic triggered by a write,
    so it must be switchable -- and provably inert when off."""
    store = Store(tmp_path / "n.db", snapshots=None)
    note_id, _ = store.create_note("a claim", "source: https://example.com/never-touched")
    assert store.read(note_id).links, "links are still indexed"
    assert store.read(note_id, with_sources=True).sources == []
    store.close()


def test_no_fetch_happens_without_a_worker(linked):
    """Capture is queued by the write and performed by the worker. Nothing in
    the write path may reach the network itself."""
    calls = []
    linked.create_note("a claim", "source: https://example.com/a")
    assert calls == []
    assert linked.snapshots.pending(), "queued, not fetched"

    SnapshotWorker(linked.snapshots, fetcher=lambda t: (calls.append(t), ok()(t))[1]).run_once()
    assert calls == ["https://example.com/a"]


def test_the_default_fetcher_is_never_invoked_by_a_write(tmp_path, monkeypatch):
    """Guards the mistake that would make the test suite phone home."""
    import seshat.snapshots as module

    def forbidden(*a, **kw):
        raise AssertionError("a write must not perform a fetch")

    monkeypatch.setattr(module, "http_fetch", forbidden)
    snaps = SnapshotStore(tmp_path / "s.db")
    store = Store(tmp_path / "n.db", snapshots=snaps)
    store.create_note("a claim", "https://example.com/a")
    store.close()
    snaps.close()
