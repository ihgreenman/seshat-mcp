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


# ------------------------------------------------- a capture that cannot be read


def _unreadable(monkeypatch, marker=b"POISON"):
    """Make extraction raise for one specific body, leaving others alone."""
    import seshat.extract as extract_module

    original = extract_module.extract

    def selective(body, content_type=None):
        if marker in body:
            raise RuntimeError("parser blew up")
        return original(body, content_type)

    monkeypatch.setattr(extract_module, "extract", selective)


def test_one_unreadable_capture_does_not_stall_the_others(tmp_path, monkeypatch):
    """§3.6: the deadline is on DETECTION. Every row behind a stalled one is a
    recovery window closing unwatched, so a row that cannot be read must be
    stepped over rather than returned on."""
    snaps = SnapshotStore(tmp_path / "s.db")
    bad = b"<html><body>POISON" + b"x" * 3000 + b"</body></html>"
    good = b"<html><body><p>" + b"readable prose. " * 200 + b"</p></body></html>"
    for note_id, body in (("aaa", bad), ("bbb", good)):
        snaps.enqueue(note_id, [(f"http://{note_id}", None, None)])
        snaps.record(note_id, f"http://{note_id}",
                     Fetched(status="ok", content_type="text/html", body=body))

    _unreadable(monkeypatch)
    assert snaps.extraction_backlog() == 2
    snaps.drain_extraction()

    # The readable one was read despite being queued behind the other.
    row = snaps.db.execute(
        "SELECT text, extraction FROM snapshot WHERE note_id = 'bbb'"
    ).fetchone()
    assert "readable prose" in row["text"]
    assert row["extraction"].startswith("fetch:")


def test_an_unreadable_capture_leaves_the_backlog_and_surfaces_for_a_human(
    tmp_path, monkeypatch
):
    """Marked rather than skipped: the backlog is a query over `extraction IS
    NULL`, so an unmarked failure is re-selected forever."""
    snaps = SnapshotStore(tmp_path / "s.db")
    body = b"<html><body>POISON" + b"x" * 3000 + b"</body></html>"
    snaps.enqueue("aaa", [("http://aaa", None, None)])
    snaps.record("aaa", "http://aaa",
                 Fetched(status="ok", content_type="text/html", body=body))

    _unreadable(monkeypatch)
    snaps.drain_extraction()

    assert snaps.extraction_backlog() == 0, "a marked failure must leave the backlog"
    row = snaps.db.execute("SELECT status, extraction FROM snapshot").fetchone()
    assert row["extraction"].startswith("failed:")
    # Bytes held, nothing readable, target possibly still live: that is triage.
    assert row["status"] == "thin"
    assert ("aaa", "http://aaa") in {
        (f["note_id"], f["target"]) for f in snaps.failures()
    }


def test_a_failed_reading_can_still_be_pasted_over(tmp_path, monkeypatch):
    """The row a human most needs to repair. A `failed:` marker holds no witness
    and re-running reproduces it exactly, so it is replaceable under the same
    rule that protects pasted and browser readings."""
    snaps = SnapshotStore(tmp_path / "s.db")
    body = b"<html><body>POISON" + b"x" * 3000 + b"</body></html>"
    snaps.enqueue("aaa", [("http://aaa", None, None)])
    snaps.record("aaa", "http://aaa",
                 Fetched(status="ok", content_type="text/html", body=body))
    _unreadable(monkeypatch)
    snaps.drain_extraction()

    assert snaps.paste("aaa", "http://aaa", "what the page actually said")
    row = snaps.db.execute("SELECT text, extraction, status FROM snapshot").fetchone()
    assert row["text"] == "what the page actually said"
    assert row["extraction"] == "manual"
    assert row["status"] == "ok"
    # And the bytes the fetcher received are untouched, as always.
    assert snaps.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == 1


def test_a_failed_reading_is_retried_by_reset_extraction(tmp_path, monkeypatch):
    """A better extractor is the other recovery path, and it must reach these
    rows: that is the whole reason the bytes were kept."""
    snaps = SnapshotStore(tmp_path / "s.db")
    body = b"<html><body>POISON<p>" + b"real text here. " * 100 + b"</p></body></html>"
    snaps.enqueue("aaa", [("http://aaa", None, None)])
    snaps.record("aaa", "http://aaa",
                 Fetched(status="ok", content_type="text/html", body=body))
    _unreadable(monkeypatch)
    snaps.drain_extraction()
    assert snaps.db.execute("SELECT extraction FROM snapshot").fetchone()[0].startswith(
        "failed:"
    )

    monkeypatch.undo()  # the better extractor arrives
    assert snaps.reset_extraction() == 1
    assert snaps.drain_extraction() == 1
    row = snaps.db.execute("SELECT text, extraction FROM snapshot").fetchone()
    assert "real text here" in row["text"]
    assert row["extraction"].startswith("fetch:")


def test_a_row_that_cannot_even_be_marked_does_not_stall_the_rest(
    tmp_path, monkeypatch
):
    """Defence in depth for the marking above. If recording the failure itself
    fails, `extract_one` returns False -- and the loop must still step over it.
    Without this case the early-return bug is untestable, because marking makes
    the failure path return True and the branch unreachable.
    """
    snaps = SnapshotStore(tmp_path / "s.db")
    good = b"<html><body><p>" + b"readable prose. " * 200 + b"</p></body></html>"
    bad = b"<html><body>POISON" + b"x" * 3000 + b"</body></html>"
    # 'aaa' sorts first, so the unmarkable row is reached first in the batch.
    for note_id, body in (("aaa", bad), ("bbb", good)):
        snaps.enqueue(note_id, [(f"http://{note_id}", None, None)])
        snaps.record(note_id, f"http://{note_id}",
                     Fetched(status="ok", content_type="text/html", body=body))

    _unreadable(monkeypatch)
    real_store = snaps.store_extraction

    def refuse_to_mark(note_id, target, extraction, status=None):
        if extraction.extractor.startswith("failed:"):
            return False  # could not even record the failure
        return real_store(note_id, target, extraction, status)

    monkeypatch.setattr(snaps, "store_extraction", refuse_to_mark)
    snaps.drain_extraction()

    row = snaps.db.execute(
        "SELECT text FROM snapshot WHERE note_id = 'bbb'"
    ).fetchone()
    assert row["text"] and "readable prose" in row["text"], (
        "a row that could not be marked must be stepped over, not returned on"
    )


# ------------------------------------------------------- outbound target policy


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("http://127.0.0.1:8765/note/x", "loopback"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://192.168.1.1/admin", "private"),
        ("http://10.0.0.5/", "private"),
        ("http://[::1]:8765/", "loopback"),
        ("http://[::ffff:127.0.0.1]/", "loopback"),
        ("http://[::ffff:169.254.169.254]/", "link-local"),
        ("http://100.64.0.1/", "carrier-grade"),
        ("http://0.0.0.0/", "unspecified"),
        ("ftp://example.com/x", "scheme"),
        ("http:///nohost", "host"),
    ],
)
def test_private_targets_are_refused(url, fragment):
    """Note text drives outbound requests, and note text is often written by a
    model summarising a page it just read. Without this a note is a
    request-forgery primitive aimed at whatever network the machine can see."""
    from seshat.snapshots import refuse_target

    objection = refuse_target(url)
    assert objection is not None, f"{url} should have been refused"
    assert fragment in objection


def _resolves_to(monkeypatch, address):
    """Pin name resolution. Keeps the suite off the network -- a test that does
    a real DNS lookup is a test that fails on a train."""
    import socket

    def fake(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_a_public_target_is_allowed(monkeypatch):
    """The guard must not refuse the thing it exists to permit."""
    from seshat.snapshots import refuse_target

    _resolves_to(monkeypatch, "93.184.216.34")
    assert refuse_target("https://example.com/page") is None


def test_a_name_resolving_into_private_space_is_refused(monkeypatch):
    """The name says nothing; the address does. A public hostname pointing at
    169.254.169.254 is the ordinary way this attack is delivered."""
    from seshat.snapshots import refuse_target

    _resolves_to(monkeypatch, "169.254.169.254")
    objection = refuse_target("https://totally-normal.example/")
    assert objection is not None and "link-local" in objection


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    """Fail closed, exactly as the bind rule does: an address whose reach cannot
    be established is not a target."""
    import socket

    from seshat.snapshots import refuse_target

    def fail(*args, **kwargs):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert refuse_target("https://nope.example/") is not None


def test_a_name_with_one_private_address_among_several_is_refused(monkeypatch):
    """Every resolved address must pass, not merely one: a name answering with
    both would otherwise connect to whichever the resolver hands over next."""
    import socket

    from seshat.snapshots import refuse_target

    def both(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port or 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", both)
    assert refuse_target("https://split-horizon.example/") is not None


def test_a_refused_target_records_a_status_and_reaches_triage(tmp_path):
    """Refusal is a capture outcome, not a crash: the note is already written,
    and a human who meant it can paste the content."""
    import time

    from seshat.snapshots import http_fetch

    # The refusal must be a DECISION, not a timeout. An unguarded fetch of a
    # metadata address reaches the same `unreachable` status by simply failing,
    # so status alone cannot tell the guard working from the guard absent --
    # the reason and the speed are what distinguish them.
    started = time.monotonic()
    direct = http_fetch("http://169.254.169.254/latest/meta-data/", timeout=20)
    assert "refused" in (direct.error or ""), "no refusal recorded; was it just unreachable?"
    assert time.monotonic() - started < 2.0, "refused before connecting, not after"

    snaps = SnapshotStore(tmp_path / "s.db")
    snaps.enqueue("aaa", [("http://169.254.169.254/latest/meta-data/", None, None)])
    SnapshotWorker(snaps, max_attempts=1).run_once()

    row = snaps.db.execute("SELECT status FROM snapshot").fetchone()
    assert row["status"] == "unreachable"
    assert snaps.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == 0, (
        "nothing may be stored from a refused target"
    )
    assert ("aaa", "http://169.254.169.254/latest/meta-data/") in {
        (f["note_id"], f["target"]) for f in snaps.failures()
    }


def test_a_redirect_into_private_space_is_refused():
    """The case a check on the original URL alone misses entirely: a public URL
    that 302s inward. The destination is what would be fetched and stored.

    Driven through urllib's real redirect machinery against a real redirecting
    server. The entry check is stepped around deliberately -- the local server
    can only live on loopback, which `http_fetch` refuses outright, so going in
    at the opener is the only way to exercise the hop rather than the entrance.
    """
    import http.server
    import threading
    import urllib.request

    from seshat.snapshots import USER_AGENT, RefusedTarget, _LimitedRedirects

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        opener = urllib.request.build_opener(_LimitedRedirects())
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"User-Agent": USER_AGENT}
        )
        with pytest.raises(RefusedTarget) as raised:
            opener.open(request, timeout=5)
    finally:
        server.shutdown()

    assert "169.254.169.254" in str(raised.value)


def test_a_redirect_to_a_public_target_still_follows(monkeypatch):
    """The guard must not break ordinary redirects, which are most of the web.

    Exercised at `redirect_request` rather than through a server: any server
    this suite can start is on loopback, which the policy refuses by design, so
    a live round trip cannot distinguish the guard working from the guard
    over-firing.
    """
    import urllib.request

    from seshat.snapshots import _LimitedRedirects

    _resolves_to(monkeypatch, "93.184.216.34")
    request = urllib.request.Request("https://example.com/moved")
    followed = _LimitedRedirects().redirect_request(
        request, None, 302, "Found", {}, "https://example.com/landed"
    )
    assert followed is not None
    assert followed.full_url == "https://example.com/landed"


def test_reference_style_anchor_text_reaches_the_expectation(tmp_path):
    """§6.9's implementation consequence, stated end to end rather than at the
    extractor: what matters is not that `link.label` is populated but that the
    witness is judged against what the citation actually said it wanted.

    Reading it off `link` would not do -- §6.9 requires the expectation be
    COPIED into `snapshot`, because `link` is derived and re-extractable, and a
    later revision could leave a 2024 capture judged against 2026 intent.
    """
    snaps = SnapshotStore(snapshot_path_for(tmp_path / "n.db"))
    store = Store(tmp_path / "n.db", snapshots=snaps)
    store.create_note(
        "API ceilings",
        "Inline [texas population data](https://example.gov/tx).\n\n"
        "Reference: see [rate limits][rl] for the ceiling.\n\n"
        "Autolink: <https://example.com/auto>\n\n"
        "[rl]: https://example.com/limits\n",
    )
    queued = {
        row["target"]: (row["expectation"], row["expectation_source"])
        for row in snaps.db.execute(
            "SELECT target, expectation, expectation_source FROM fetch_queue"
        )
    }
    assert queued["https://example.gov/tx"] == ("texas population data", "anchor")
    assert queued["https://example.com/limits"] == ("rate limits", "anchor")
    # An autolink carries no anchor text, so it takes the fallback -- correctly.
    assert queued["https://example.com/auto"][1] != "anchor"
