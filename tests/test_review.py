"""The local review interface. Spec §10.

Driven over real HTTP against a real server, because the properties that matter
here are properties of the interface -- what it refuses, what it cannot do --
and testing the render functions directly would skip exactly those.
"""

import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from seshat.review import Handler, Review, serve_review
from seshat.snapshots import Fetched, SnapshotStore, SnapshotWorker, snapshot_path_for
from seshat.store import Store


@pytest.fixture
def populated(tmp_path):
    notes = tmp_path / "n.db"
    snaps = SnapshotStore(snapshot_path_for(notes))
    store = Store(notes, snapshots=snaps)

    original, _ = store.create_note(
        "Prewarping is unnecessary", "the error is below the noise floor"
    )
    current, _ = store.create_note(
        "Prewarping is required after all", "measured 24% error at 16 kHz",
        [{"id": original, "retained": 0.0, "why": "measurement contradicts it"}],
    )
    store.create_note("Follow-up", f"builds on {current}")
    store.create_note(
        "cites a page", "see [texas population data](https://example.gov/tx) for figures"
    )
    SnapshotWorker(snaps, fetcher=lambda t: Fetched(
        status="ok", http_status=200, content_type="text/html",
        body=b"<html><title>Subscribe</title><body><p>Subscribe to continue reading. "
             b"Sign in to your account. Members get unlimited access to everything.</p></body></html>",
    )).run_once()
    store.close()
    snaps.close()
    return notes, original, current


@pytest.fixture
def server(populated):
    notes, original, current = populated
    review = Review(notes)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("B", (Handler,), {"review": review}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, review, original, current
    httpd.shutdown()
    review.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are part of what is being asserted -- following them silently
    turns a 303 into a 200 and hides which branch ran."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch(request):
    try:
        with _OPENER.open(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def get(base, path):
    return _fetch(urllib.request.Request(base + path))


def post(base, path, fields, origin=None):
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(base + path, data=data, method="POST")
    if origin:
        request.add_header("Origin", origin)
    return _fetch(request)


# ------------------------------------------------------- §10.1 reading


def test_every_reading_route_serves(server):
    base, _, original, _ = server
    for path in ("/", "/notes", "/notes?mode=heads", "/notes?mode=open",
                 "/notes?mode=cited", f"/note/{original}", f"/chain/{original}",
                 "/triage", "/check", "/search?q=prewarping"):
        status, body = get(base, path)
        assert status == 200, path
        assert "seshat" in body


def test_a_note_page_shows_its_supersession_state(server):
    base, _, original, current = server
    _, body = get(base, f"/note/{original}")
    assert "has been superseded" in body
    assert current in body, "and points at the current head"


def test_open_loops_and_citations_are_offered_as_entry_points(server):
    """§8: the only mitigation with a mechanism is reading the store in full,
    and these are the two handles the spec names for where to start."""
    base, _, _, _ = server
    _, home = get(base, "/")
    assert "/notes?mode=open" in home
    assert "/notes?mode=cited" in home
    assert get(base, "/notes?mode=open")[0] == 200


def test_unknown_routes_are_404_not_500(server):
    base, _, _, _ = server
    assert get(base, "/nonexistent")[0] == 404


def test_a_bad_note_id_reports_rather_than_crashing(server):
    base, _, _, _ = server
    status, body = get(base, "/note/zoo-zoo-zoo-zoo")
    assert status == 500 and "context()" in body, "the store's guidance reaches the page"


# ---------------------------------------------- §10.3 no edit affordance


def test_the_notes_database_is_opened_read_only(server):
    """§10.3: the UI must not permit editing a note, and the absence will feel
    like an oversight to someone reasonable. A connection that physically
    cannot write is not something anyone relaxes by accident."""
    import sqlite3

    _, review, _, _ = server
    assert review.store.read_only is True
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        review.store.db.execute("UPDATE note SET desc = 'edited'")


def test_no_route_offers_note_editing(server):
    base, _, original, _ = server
    _, body = get(base, f"/note/{original}")
    # The header's search form is a GET; nothing on a note page posts anywhere.
    note_body = body.split("</header>", 1)[1]
    assert "method=\"post\"" not in note_body and "<textarea" not in note_body
    assert "immutable" in body, "and it says why, since the absence looks like an oversight"


def test_reading_the_store_does_not_write_to_it(server, populated):
    """A reader observing an id miss must not become a writer of one."""
    notes, _, _ = populated
    base, _, _, _ = server
    before = notes.read_bytes()
    get(base, "/search?q=zoo-zoo-zoo-zoo")
    get(base, "/notes")
    assert notes.read_bytes() == before


# ------------------------------------------------------ §10.2 triage


def test_triage_lists_what_needs_a_human(server):
    base, _, _, _ = server
    _, body = get(base, "/triage")
    assert "expectation" in body
    assert "example.gov/tx" in body


def test_pasting_fills_a_failed_capture(server):
    """§10.2: the user is already authenticated and already looking at the page,
    so a paste box clears the whole paywall class without seshat ever handling
    a credential."""
    base, review, _, _ = server
    note_id = next(
        r["note_id"] for r in review.snapshots.unmet_expectations()
    )
    status, _ = post(base, "/paste", {
        "token": review.token, "note_id": note_id,
        "target": "https://example.gov/tx", "text": "Texas population data, by county.",
    })
    assert status == 303

    source = next(s for s in review.snapshots.sources_for(note_id)
                  if s["target"] == "https://example.gov/tx")
    assert source["text"] == "Texas population data, by county."
    assert source["provenance"] == "manual", "human-mediated provenance survives in the data"
    assert source["expectation_met"] is True


def test_acknowledging_removes_a_target_from_the_queue(server):
    """§10.4: record the failure durably and stop retrying. The note remains
    valid -- it cites a source nobody can check, which is weaker evidence and
    not a defect."""
    base, review, _, _ = server
    review.snapshots.record("x-y-z-w", "https://dead.example/gone",
                            Fetched(status="gone", http_status=410))
    assert any(r["target"] == "https://dead.example/gone"
               for r in review.snapshots.failures())

    status, _ = post(base, "/acknowledge", {
        "token": review.token, "note_id": "x-y-z-w", "target": "https://dead.example/gone",
    })
    assert status == 303
    assert not any(r["target"] == "https://dead.example/gone"
                   for r in review.snapshots.failures())
    assert review.snapshots.histogram()["acknowledged"] == 1


def test_a_good_capture_cannot_be_retried_away(server):
    """Retry deletes and re-fetches, so it must refuse anything that succeeded
    -- a successful capture is immutable (§10.3)."""
    _, review, _, _ = server
    review.snapshots.record("a-b-c-d", "https://example.com/fine",
                            Fetched(status="ok", http_status=200, body=b"<p>content</p>"))
    assert review.snapshots.retry("a-b-c-d", "https://example.com/fine") is False


def test_pasting_replaces_a_machine_reading_but_not_the_bytes(server):
    """The rule that resolves §10.3 for the paywall case, stated as a test.

    What is immutable is the *witness*: the bytes the fetcher received. A
    machine extraction is a derived reading of those bytes and can always be
    regenerated, so a human replacing it loses nothing -- and provenance records
    who read it. The interpretation is mine, not the spec's letter; §10.3 says
    "a snapshot that already holds content is immutable", and a paywall
    interstitial does hold content.
    """
    base, review, _, _ = server
    review.snapshots.record("a-b-c-d", "https://example.com/has-text",
                            Fetched(status="ok", http_status=200, body=b"<p>original</p>"))
    review.snapshots.extract_one("a-b-c-d", "https://example.com/has-text",
                                 b"<p>original</p>", "text/html")
    raw_before = review.snapshots.db.execute(
        "SELECT body FROM raw WHERE note_id = ? AND target = ?",
        ("a-b-c-d", "https://example.com/has-text"),
    ).fetchone()["body"]

    post(base, "/paste", {
        "token": review.token, "note_id": "a-b-c-d",
        "target": "https://example.com/has-text", "text": "replacement",
    })
    source = next(s for s in review.snapshots.sources_for("a-b-c-d")
                  if s["target"] == "https://example.com/has-text")
    assert source["text"] == "replacement"
    assert source["provenance"] == "manual", "who read it survives in the data"

    raw_after = review.snapshots.db.execute(
        "SELECT body FROM raw WHERE note_id = ? AND target = ?",
        ("a-b-c-d", "https://example.com/has-text"),
    ).fetchone()["body"]
    assert raw_after == raw_before, "the witness itself is untouched"


# --------------------------------------------------- the trust boundary


def test_a_post_without_the_token_is_refused(server):
    """Any page you visit can post to 127.0.0.1. "localhost is safe" stops
    being true the moment a browser is a client."""
    base, review, _, _ = server
    status, _ = post(base, "/acknowledge", {
        "note_id": "a-b-c-d", "target": "https://example.com/x",
    })
    assert status == 403
    assert review.snapshots.histogram()["acknowledged"] == 0


def test_a_cross_origin_post_is_refused(server):
    base, review, _, _ = server
    status, _ = post(base, "/acknowledge", {
        "token": review.token, "note_id": "a-b-c-d", "target": "https://example.com/x",
    }, origin="https://evil.example")
    assert status == 403
    assert review.snapshots.histogram()["acknowledged"] == 0


def test_a_same_origin_post_is_accepted(server):
    base, review, _, _ = server
    host = base.removeprefix("http://")
    status, _ = post(base, "/acknowledge", {
        "token": review.token, "note_id": "a-b-c-d", "target": "https://example.com/x",
    }, origin=f"http://{host}")
    assert status == 303


def test_binding_beyond_localhost_is_refused(tmp_path):
    """§10/§12: remote transport is out of scope, and binding wider would demand
    an auth model this tool has no business owning."""
    Store(tmp_path / "n.db").close()
    with pytest.raises(ValueError, match="localhost-only"):
        serve_review(tmp_path / "n.db", host="0.0.0.0", snapshots=False)


def test_note_text_is_escaped(tmp_path):
    """Server-rendered HTML from arbitrary note text; the store holds whatever
    was written."""
    notes = tmp_path / "x.db"
    store = Store(notes)
    nasty, _ = store.create_note("<script>alert(1)</script>", "<img src=x onerror=alert(2)>")
    store.close()

    review = Review(notes, snapshots=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("B", (Handler,), {"review": review}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _, body = get(base, f"/note/{nasty}")
        # Escaped, so inert: the angle brackets are what make markup markup.
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<img src=x" not in body
        assert "&lt;img src=x onerror=alert(2)&gt;" in body
    finally:
        httpd.shutdown()
        review.close()


def test_pasting_over_a_paywall_keeps_the_captured_bytes(server):
    """§10.3's line, precisely. An interstitial IS content -- a truthful record
    of what the URL served an anonymous fetcher -- so replacing the reading
    must not destroy it. `raw` is never touched: both witnesses survive."""
    base, review, _, _ = server
    note_id = next(r["note_id"] for r in review.snapshots.unmet_expectations())
    before = review.snapshots.db.execute(
        "SELECT body_hash FROM raw WHERE note_id = ? AND target = ?",
        (note_id, "https://example.gov/tx"),
    ).fetchone()["body_hash"]

    post(base, "/paste", {
        "token": review.token, "note_id": note_id,
        "target": "https://example.gov/tx", "text": "Texas population data, by county.",
    })

    after = review.snapshots.db.execute(
        "SELECT body_hash FROM raw WHERE note_id = ? AND target = ?",
        (note_id, "https://example.gov/tx"),
    ).fetchone()["body_hash"]
    assert after == before, "the paywall bytes remain as captured"


def test_a_manual_paste_is_never_replaced_by_another(server):
    """Pasted text cannot be regenerated from anything, so overwriting it is
    the real loss §10.3 forbids -- unlike a machine reading, which `raw` can
    always reproduce."""
    base, review, _, _ = server
    note_id = next(r["note_id"] for r in review.snapshots.unmet_expectations())
    fields = {"token": review.token, "note_id": note_id, "target": "https://example.gov/tx"}
    post(base, "/paste", {**fields, "text": "first paste, carefully checked"})
    post(base, "/paste", {**fields, "text": "second paste, careless"})

    source = next(s for s in review.snapshots.sources_for(note_id)
                  if s["target"] == "https://example.gov/tx")
    assert source["text"] == "first paste, carefully checked"
