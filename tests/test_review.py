"""The local review interface. Spec §10.

Driven over real HTTP against a real server, because the properties that matter
here are properties of the interface -- what it refuses, what it cannot do --
and testing the render functions directly would skip exactly those.
"""

import json
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


# ------------------------------------------- §6.10 / §10.2 the extension API


def api(base, path, payload=None, token=None, origin=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method or ("POST" if data else "GET")
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if origin:
        request.add_header("Origin", origin)
    if data:
        request.add_header("Content-Type", "application/json")
    status, body = _fetch(request)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, {"raw": body}


def test_the_api_requires_a_token(server):
    base, review, _, _ = server
    assert api(base, "/api/ping")[0] == 401
    assert api(base, "/api/ping", token="wrong")[0] == 401
    assert api(base, "/api/ping", token=review.api_token)[0] == 200


def test_an_ordinary_web_page_cannot_reach_the_api(server):
    """Token plus origin, per §10.2. Even holding the token, a page on the open
    web is not a caller this endpoint recognises."""
    base, review, _, _ = server
    status, _ = api(base, "/api/ping", token=review.api_token,
                    origin="https://evil.example")
    assert status == 401


def test_an_extension_origin_is_accepted(server):
    base, review, _, _ = server
    status, body = api(base, "/api/ping", token=review.api_token,
                       origin="chrome-extension://abcdefghijklmnop")
    assert status == 200 and body["capture"] is True


def test_capture_writes_a_note_with_the_page_attached(server):
    """§6.10: the extension is a second front door to note(), not a new model.
    What it writes is a note with evidence attached -- not an unattached capture
    awaiting a note."""
    base, review, _, _ = server
    status, body = api(base, "/api/capture", {
        "desc": "Prewarping is exact at only one frequency",
        "detail": "the match holds at the chosen wc and nowhere else",
        "url": "https://example.org/bilinear",
        "title": "Bilinear transform",
        "text": "The bilinear transform compresses the frequency axis. Prewarping is "
                "exact at only one frequency, the one you choose, and nowhere else.",
        "html": "<html><body><p>Prewarping is exact at only one frequency.</p></body></html>",
    }, token=review.api_token)

    assert status == 200
    note_id = body["id"]
    record = review.store.read(note_id)
    assert record.desc == "Prewarping is exact at only one frequency"
    assert "https://example.org/bilinear" in record.text

    source = review.snapshots.sources_for(note_id)[0]
    assert source["provenance"] == "browser", "§10.2 provenance is three-valued"
    assert "compresses the frequency axis" in source["text"]


def test_a_browser_capture_never_queues_a_fetch(server):
    """§6.10's main argument for building this: the browser supplies content at
    write time, so the gap that rot and drift live in is zero. `pending` never
    rises on this path and the recovery window never opens."""
    base, review, _, _ = server
    before = len(review.snapshots.pending())
    api(base, "/api/capture", {
        "desc": "a finding", "url": "https://example.org/live",
        "text": "Plenty of real content on this page, captured from the rendered DOM.",
        "html": "<html><body>content</body></html>",
    }, token=review.api_token)

    assert len(review.snapshots.pending()) == before, "nothing queued"
    assert review.snapshots.histogram()["pending"] == before


def test_capture_sets_the_expectation_from_what_you_said(server):
    """The desc is both the note's handle and §6.9's expectation -- you say what
    the page told you, and the capture is immediately checkable against it."""
    base, review, _, _ = server
    _, body = api(base, "/api/capture", {
        "desc": "wireguard handshake fails behind symmetric NAT",
        "url": "https://example.org/wg",
        "text": "The wireguard handshake fails behind symmetric NAT unless one peer "
                "has a stable endpoint or you run a relay.",
        "html": "<html><body>x</body></html>",
    }, token=review.api_token)

    source = review.snapshots.sources_for(body["id"])[0]
    assert source["expectation"] == "wireguard handshake fails behind symmetric NAT"
    assert source["expectation_met"] is True


def test_a_thin_browser_capture_is_reported_back_immediately(server):
    """`thin` is decidable at once on this path, so the popup can say so while
    you are still looking at the page."""
    base, review, _, _ = server
    _, body = api(base, "/api/capture", {
        "desc": "a finding", "url": "https://example.org/spa",
        "text": "", "html": "<html><body><div id=root></div>" + "x" * 5000 + "</body></html>",
    }, token=review.api_token)
    assert body["snapshot_status"] == "thin"


def test_capture_requires_a_desc(server):
    """§6.10: the extension should prompt for what the page told you. A capture
    with nothing said about it is a bookmark."""
    base, review, _, _ = server
    status, _ = api(base, "/api/capture", {"url": "https://example.org/x"},
                    token=review.api_token)
    assert status == 400


def test_the_api_cannot_edit_a_note(server):
    """§10.3 holds on this path too: creation is a front door to note(),
    updating is not a thing that exists anywhere."""
    base, review, _, _ = server
    _, body = api(base, "/api/capture", {
        "desc": "original", "url": "https://example.org/a", "text": "content here",
    }, token=review.api_token)
    for action in ("update", "edit", "delete"):
        assert api(base, f"/api/{action}", {"id": body["id"]},
                   token=review.api_token)[0] == 404


def test_repair_uses_the_page_you_are_looking_at(server):
    """§10.2's other job: something failed, go to the page, click."""
    base, review, _, _ = server
    note_id = next(r["note_id"] for r in review.snapshots.unmet_expectations())
    status, body = api(base, "/api/repair", {
        "note_id": note_id, "target": "https://example.gov/tx",
        "text": "Texas population data by county, 2020 through 2025.",
        "html": "<html><body>real content</body></html>",
    }, token=review.api_token)

    assert status == 200 and body["ok"] is True
    source = next(s for s in review.snapshots.sources_for(note_id)
                  if s["target"] == "https://example.gov/tx")
    assert source["provenance"] == "browser"
    assert source["expectation_met"] is True


def test_targets_tells_the_extension_what_needs_repair(server):
    base, review, _, _ = server
    status, body = api(base, "/api/targets", token=review.api_token)
    assert status == 200
    assert any(t["target"] == "https://example.gov/tx" for t in body["targets"])


def test_the_api_can_be_refused_entirely(tmp_path):
    """A user who only wants to browse should not be running a write endpoint."""
    Store(tmp_path / "n.db").close()
    review = Review(tmp_path / "n.db", snapshots=False, capture_api=False)
    try:
        assert review.writer is None
        assert review.api_token is None
    finally:
        review.close()


def test_the_token_file_is_not_world_readable(tmp_path):
    from seshat.review import load_token, token_path

    Store(tmp_path / "n.db").close()
    load_token(tmp_path / "n.db")
    assert token_path(tmp_path / "n.db").stat().st_mode & 0o077 == 0


def test_a_human_reading_is_never_replaced_by_another(server):
    """The rule stated once in `_clear_machine_reading`: replaceable exactly
    when regenerable, and only a fetch reading is. A browser reading that
    repaired a fetch is NOT regenerable -- `raw` still holds the fetcher's
    bytes, so re-extracting would give back the paywall, not the reading."""
    base, review, _, _ = server
    note_id = next(r["note_id"] for r in review.snapshots.unmet_expectations())
    target = "https://example.gov/tx"

    api(base, "/api/repair", {
        "note_id": note_id, "target": target,
        "text": "The careful reading, taken with the page open.",
        "html": "<html><body>real</body></html>",
    }, token=review.api_token)

    # A second attempt, browser or paste, must not overwrite it.
    api(base, "/api/repair", {
        "note_id": note_id, "target": target, "text": "a careless second pass",
    }, token=review.api_token)
    assert review.snapshots.paste(note_id, target, "and a careless third") is False

    source = next(s for s in review.snapshots.sources_for(note_id) if s["target"] == target)
    assert source["text"] == "The careful reading, taken with the page open."


def test_a_repair_leaves_the_fetchers_bytes_as_captured(server):
    """What the URL served an anonymous fetcher stays on the record even after
    a human supplies a better reading. Both witnesses survive."""
    base, review, _, _ = server
    note_id = next(r["note_id"] for r in review.snapshots.unmet_expectations())
    target = "https://example.gov/tx"
    before = review.snapshots.db.execute(
        "SELECT body_hash FROM raw WHERE note_id = ? AND target = ?", (note_id, target)
    ).fetchone()["body_hash"]

    api(base, "/api/repair", {
        "note_id": note_id, "target": target, "text": "the real article",
        "html": "<html><body>real</body></html>",
    }, token=review.api_token)

    after = review.snapshots.db.execute(
        "SELECT body_hash FROM raw WHERE note_id = ? AND target = ?", (note_id, target)
    ).fetchone()["body_hash"]
    assert after == before
