"""Snapshot capture. Spec §6.6, §6.9, §3.6.

Capture and extraction are separate stages, and both now run: capture because
an unfetched page may be gone forever, extraction because `thin` cannot be
assessed without it and `thin` is the status with a deadline.

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
    assert source["extracted"] is True
    assert source["provenance"] == "fetch", "§10.2 provenance is three-valued"


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

    linked.snapshots.enqueue(note_id, [("https://example.com/a", None, None)])
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
    assert histogram["thin"] == 0, "nothing captured here was thin"
    assert histogram["unextracted"] == 0, "extraction runs with capture now"


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


# --------------------------------------------------- §6.9 capture expectation


def test_anchor_text_becomes_the_expectation(linked):
    """§6.9: the expectation is already available and costs no new write-path
    argument -- `[texas population data](url)` states it in the act of citing."""
    note_id, _ = linked.create_note(
        "state demographics",
        "see [texas population data](https://example.gov/tx) for the figures",
    )
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<p>Texas population data: 30 million</p>")).run_once()

    source = linked.snapshots.sources_for(note_id)[0]
    assert source["expectation"] == "texas population data"
    assert source["expectation_source"] == "anchor"
    assert source["expectation_met"] is True


def test_a_degenerate_label_falls_back_to_the_note(linked):
    """'here' states no expectation, so the note's own desc is a better answer."""
    note_id, _ = linked.create_note(
        "Bilinear transform compresses the frequency axis",
        "details [here](https://example.com/x)",
    )
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<p>unrelated content</p>")).run_once()

    source = linked.snapshots.sources_for(note_id)[0]
    assert "Bilinear transform" in source["expectation"]
    assert source["expectation_source"] in ("desc", "sentence")


def test_a_paywall_fails_the_expectation_without_changing_status(linked):
    """§6.9's callout: candidate generation for review, NEVER a verdict.
    Automatic marking would produce false `thin` on the reference pages most
    worth preserving."""
    note_id, _ = linked.create_note(
        "state demographics",
        "see [texas population data](https://example.gov/tx) for the figures",
    )
    # Substantial prose, so §6.6's structural check is satisfied -- this is
    # precisely the case §6.9 exists for: what size and status cannot catch.
    wall = (b"<html><body><p>Subscribe to continue reading. Sign in to your account. "
            b"Choose a plan that works for you. Accept cookies to proceed. "
            b"Members enjoy unlimited access to our award winning journalism. "
            b"Already a subscriber? Sign in. Cancel anytime, no commitment required.</p>"
            b"</body></html>")
    SnapshotWorker(linked.snapshots, fetcher=ok(wall)).run_once()

    source = linked.snapshots.sources_for(note_id)[0]
    assert source["status"] == "ok", "structurally fine -- size cannot catch this"
    assert source["expectation_met"] is False, "but it is not what was asked for"


def test_expectation_is_copied_not_referenced(linked):
    """§6.9: `link` is derived and re-extractable, so a later revision could
    otherwise leave an old capture judged against new intent."""
    note_id, _ = linked.create_note(
        "a finding", "see [original wording](https://example.com/x) for detail"
    )
    SnapshotWorker(linked.snapshots, fetcher=ok()).run_once()
    linked.reindex_links()  # rebuild the derived index underneath it

    assert linked.snapshots.sources_for(note_id)[0]["expectation"] == "original wording"


def test_expectation_met_is_none_when_unanswerable(linked):
    """None is not False: one means 'not checked', the other 'checked and failed'."""
    from seshat.extract import expectation_met

    assert expectation_met(None, "text") is None
    assert expectation_met("something", None) is None
    assert expectation_met("the and of", "text") is None, "no content words to check"


def test_redirects_are_recorded(linked):
    """A recycled shortener produces plausible text from somewhere other than
    the cited URL -- §6.9's wrong-page case, undetectable without this."""
    note_id, _ = linked.create_note("a claim", "https://example.com/short")
    SnapshotWorker(
        linked.snapshots,
        fetcher=lambda t: Fetched(status="ok", http_status=200, content_type="text/html",
                                  body=b"<p>somewhere else entirely</p>",
                                  final_url="https://elsewhere.example/actual"),
    ).run_once()

    assert linked.snapshots.sources_for(note_id)[0]["final_url"] == "https://elsewhere.example/actual"


# ------------------------------------------------------------- extraction


def test_capture_extracts_immediately(linked):
    """The deadline is on detection, not extraction (§3.6): a thin capture can
    only be repaired while the page is live."""
    note_id, _ = linked.create_note("a claim", "https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<html><title>T</title><p>Real prose.</p></html>")).run_once()

    source = linked.snapshots.sources_for(note_id)[0]
    assert source["title"] == "T"
    assert source["text"] == "Real prose."
    assert source["extraction"].startswith("fetch:")
    assert linked.snapshots.extraction_backlog() == 0


def test_a_thin_capture_is_flagged_at_extraction(linked):
    shell = b"<html><head><title>App</title></head><body><script>" + b"x" * 5000 + b"</script></body></html>"
    note_id, _ = linked.create_note("a claim", "https://example.com/spa")
    SnapshotWorker(linked.snapshots, fetcher=ok(shell)).run_once()

    assert linked.snapshots.sources_for(note_id)[0]["status"] == "thin"
    assert linked.snapshots.histogram()["thin"] == 1


def test_reextraction_never_overwrites_pasted_content(linked):
    """§10.3: a snapshot with no content is being filled, not rewritten. Pasted
    text is a witness nothing can regenerate."""
    note_id, _ = linked.create_note("a claim", "https://example.com/paywalled")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<html>" + b"<script>x</script>" * 500 + b"</html>")).run_once()
    assert linked.snapshots.sources_for(note_id)[0]["status"] == "thin"

    assert linked.snapshots.paste(note_id, "https://example.com/paywalled", "The real article text.")
    source = linked.snapshots.sources_for(note_id)[0]
    assert source["text"] == "The real article text."
    assert source["provenance"] == "manual"
    assert source["status"] == "ok"

    linked.snapshots.reset_extraction()
    assert linked.snapshots.drain_extraction() >= 0
    after = linked.snapshots.sources_for(note_id)[0]
    assert after["text"] == "The real article text.", "manual witness survives a re-extraction"
    assert after["provenance"] == "manual"


def test_reextraction_refreshes_machine_extractions(linked):
    note_id, _ = linked.create_note("a claim", "https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<p>Original prose.</p>")).run_once()

    assert linked.snapshots.reset_extraction() == 1
    assert linked.snapshots.extraction_backlog() == 1
    assert linked.snapshots.drain_extraction() == 1
    assert linked.snapshots.sources_for(note_id)[0]["text"] == "Original prose."


def test_the_captured_bytes_are_what_is_immutable(linked):
    """§10.3, as resolved for the paywall case (see test_review.py).

    A machine extraction is a derived reading and can be regenerated from the
    stored bytes, so a human may replace it. The bytes themselves never change
    -- they are the witness, and they cannot be re-fetched.
    """
    note_id, _ = linked.create_note("a claim", "https://example.com/a")
    SnapshotWorker(linked.snapshots, fetcher=ok(b"<p>The captured text.</p>")).run_once()
    before = linked.snapshots.db.execute("SELECT body_hash FROM raw").fetchone()["body_hash"]

    assert linked.snapshots.paste(note_id, "https://example.com/a", "a human reading") is True
    assert linked.snapshots.sources_for(note_id)[0]["text"] == "a human reading"
    assert linked.snapshots.db.execute(
        "SELECT body_hash FROM raw").fetchone()["body_hash"] == before
