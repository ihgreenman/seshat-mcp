"""Snapshots: what a target said when a given note was written. Spec §6.6.

A snapshot is a witness, keyed `(note_id, target)` and immutable exactly like
the note it belongs to. Superseding a note leaves its witness intact, because
the old belief was formed against the old page.

**This is the only unrecoverable data in the system.** `link` is regenerable
from note text in seconds; a snapshot is gone forever if dropped, and the two
share a primary key. They live in SEPARATE DATABASE FILES so that a rebuild of
the derived index cannot reach this table even by accident. See
`Store._rebuild_links`.

Capture and extraction are separate stages, and the ordering was deliberate:
capture shipped first because an unfetched page may be gone forever, while
extraction re-runs over stored bytes whenever a better extractor appears.

**The deadline is on detection, not extraction** (§3.6). A capture that is a
paywall interstitial or a JS shell can only be repaired while the page is live,
and that cannot be known until extraction runs -- so every day without
extraction grows the population of captures whose recovery windows are expiring
silently.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import __version__
from .extract import expectation_met

log = logging.getLogger("seshat.snapshots")

SCHEMA_VERSION = 3

RAW_CAP = 8 * 1024 * 1024
"""Bytes retained per capture. Generous: this is the copy extraction re-runs
over, and a truncated body is a permanently degraded witness."""

FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
PROJECT_URL = "https://github.com/ihgreenman/seshat-mcp"

USER_AGENT = f"seshat/{__version__} (+{PROJECT_URL}; personal note store; capture-once)"
"""Sent to every target a note cites, so it should be true.

It used to claim 0.1 against a software version of 0.3.0, and carried a bare
`+https://github.com/` -- a placeholder that resolves to nothing and tells an
operator reading their logs less than saying nothing would. Both now come from
the one place each is defined."""

STATUSES = ("ok", "gone", "unreachable", "thin")
"""§6.6. `thin` means extraction yielded implausibly little, and is the one
status with a deadline attached: it needs human attention **while manual
recovery is still possible**."""

FETCH = "fetch"
BROWSER = "browser"
MANUAL = "manual"
"""§10.2 provenance, three-valued. The DOM after script execution is materially
different provenance from pasted text -- better fidelity, but rendered for that
viewer, personalisation and A/B bucketing included."""

FAILED_PREFIX = "failed:"
"""Marks a reading the extractor could not produce.

Not a fourth provenance -- there is no witness here -- but it has to occupy
`extraction` so the row leaves a backlog defined as `extraction IS NULL`.
Replaceable by every repair path, and by the module's own rule rather than as
an exception to it: a reading is replaceable exactly when it can be
regenerated, and re-running the extractor over the same bytes reproduces this
failure precisely. It is also the reading a human most needs to paste over."""

DEGENERATE_LABELS = frozenset({
    "here", "this", "this article", "this page", "link", "click here", "read more",
    "more", "source", "see here", "article", "paper", "docs", "documentation", "it",
})
"""Anchor text that states no expectation (§6.9), so the note's own desc is a
better answer to "what was sought here"."""

SCHEMA = """
-- A witness: what the target said when this note was written. Immutable.
-- Same key as `link`, opposite recoverability. NEVER dropped by an
-- extraction rebuild. §6.6
CREATE TABLE snapshot (
  note_id       TEXT NOT NULL,
  target        TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  status        TEXT NOT NULL,    -- 'ok' | 'gone' | 'unreachable' | 'thin'
  title         TEXT,
  text          TEXT,             -- extracted, truncated to the cap
  full_hash     TEXT,             -- over the COMPLETE extraction, not `text`
  full_length   INTEGER,
  raw_length    INTEGER,
  extraction    TEXT,             -- 'fetch' | 'browser' | 'manual' + version;
                                  -- NULL = bytes held, nothing has read them
  expectation   TEXT,             -- what was sought here, copied at capture §6.9
  expectation_source TEXT,        -- 'anchor' | 'sentence' | 'desc'; how it was derived
  -- Facts about the capture that are unrecoverable afterwards. They live here
  -- rather than only in `raw` because `raw` is the prunable half: bodies are
  -- large, and a store that drops them to save space must not thereby forget
  -- where its evidence came from.
  final_url     TEXT,             -- after redirects; NULL when it never moved
  http_status   INTEGER,
  content_type  TEXT,
  PRIMARY KEY (note_id, target)
);

-- The increment-one hedge: the bytes themselves, so extraction stays
-- re-runnable. Separate from `snapshot` so a future extractor writes the
-- derived columns without rewriting the witness.
CREATE TABLE raw (
  note_id       TEXT NOT NULL,
  target        TEXT NOT NULL,
  fetched_at    TEXT NOT NULL,
  http_status   INTEGER,
  content_type  TEXT,
  body          BLOB NOT NULL,
  body_hash     TEXT NOT NULL,    -- sha256 over the COMPLETE response body
  body_length   INTEGER NOT NULL, -- before RAW_CAP truncation
  truncated     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (note_id, target)
);

-- §10.4: some targets cannot be preserved at all -- dead before capture,
-- DRM-protected, interaction-dependent, or purely audiovisual. The correct
-- response is to record the failure durably and STOP RETRYING.
--
-- A separate table, not a column on `snapshot`: an acknowledgement is a
-- reviewer's decision about a witness, not a property of it. The witness
-- records what the world said and stays immutable; this records what a human
-- concluded about it, and the two have different authors and lifetimes.
CREATE TABLE acknowledgement (
  note_id         TEXT NOT NULL,
  target          TEXT NOT NULL,
  acknowledged_at TEXT NOT NULL,
  reason          TEXT,
  PRIMARY KEY (note_id, target)
);

-- Durable, so a crash between write and fetch does not lose a capture that
-- can never be taken again.
CREATE TABLE fetch_queue (
  note_id      TEXT NOT NULL,
  target       TEXT NOT NULL,
  enqueued_at  TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  expectation  TEXT,             -- carried to the snapshot at capture §6.9
  expectation_source TEXT,
  PRIMARY KEY (note_id, target)
);
"""


ADDITIVE = """
CREATE TABLE IF NOT EXISTS acknowledgement (
  note_id         TEXT NOT NULL,
  target          TEXT NOT NULL,
  acknowledged_at TEXT NOT NULL,
  reason          TEXT,
  PRIMARY KEY (note_id, target)
);
"""
"""Tables a newer build adds to an existing snapshot database. Creation only --
anything that would rewrite a witness belongs in a reset, not here."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Fetched:
    """One capture attempt. `body` is None unless something was retrieved."""

    status: str  # one of STATUSES
    http_status: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    error: str | None = None
    final_url: str | None = None
    """Where the content actually came from after redirects.

    A recycled shortener or a "this content has moved" hop produces perfectly
    plausible text from somewhere other than the cited URL -- §6.9's wrong-page
    case, and undetectable without recording this at capture time."""


def snapshot_path_for(notes_db: str | Path) -> str:
    """Snapshots live outside the notes store, which stays small and portable."""
    text = str(notes_db)
    if text == ":memory:":
        return ":memory:"
    path = Path(text)
    return str(path.with_name(path.name + ".snapshots.db"))


CGNAT = ipaddress.ip_network("100.64.0.0/10")
"""RFC 6598 shared address space. Not `is_private` by Python's reckoning, but
not reachable on the public internet either -- it is the carrier-grade NAT
range, so a target there is somewhere inside an ISP, not on the web."""


class RefusedTarget(Exception):
    """A capture target that resolves somewhere it must not be fetched from."""


def _address_objection(address) -> str | None:
    """Why this address is not a public capture target, or None if it is one."""
    if address.is_unspecified:
        return "the unspecified address"
    if address.is_loopback:
        return "a loopback address"
    if address.is_link_local:
        return "a link-local address (cloud metadata lives here)"
    if address.is_multicast:
        return "a multicast address"
    if address.is_private or address.is_reserved:
        return "a private or reserved address"
    if address.version == 4 and address in CGNAT:
        return "carrier-grade NAT space"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        # Belt and braces: Python classifies mapped forms correctly today, and
        # the spec (§10) names them as the usual way such a check goes wrong.
        return _address_objection(mapped)
    return None


def refuse_target(url: str) -> str | None:
    """Why `url` must not be fetched, or None if it may be.

    **Note text drives this.** A write makes outbound requests (§6.6), and note
    text is frequently written by a model summarising a page it just read -- so
    an attacker-influenced document reaches the fetcher with one hop of
    laundering. Without this, a note is a request-forgery primitive: the fetcher
    runs on the user's machine, inside whatever network that machine can see,
    and the body it retrieves is stored and later rendered in the review UI.

    Refusal is by resolution, and there is no override -- the same rule §10
    applies to the bind address, pointed outward. Somebody who genuinely wants a
    private page preserved can paste it (§10.2); that path was already built,
    already requires a human, and never puts seshat on the private network.

    **What this does not stop:** the name is resolved here and again by the
    connection, so a DNS answer that changes between the two is not caught.
    Closing that needs connecting to a pinned address with an explicit Host
    header and certificate check, which is disproportionate here -- the attacker
    would need to control DNS for a name a note already cites. Recorded rather
    than implied, because a guard nobody knows the limits of gets trusted past
    them.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return f"unsupported scheme: {parsed.scheme or url[:40]!r}"
    host = parsed.hostname
    if not host:
        return "no host in the URL"
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"{host} does not resolve ({exc.strerror or exc})"
    if not infos:
        return f"{host} resolves to nothing"
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        objection = _address_objection(address)
        if objection is not None:
            # Every resolved address must be acceptable, not merely one: a name
            # answering with both a public and a private address would otherwise
            # pass here and connect to whichever the resolver hands over next.
            return f"{host} resolves to {address}, {objection}"
    return None


def http_fetch(target: str, timeout: float = FETCH_TIMEOUT) -> Fetched:
    """Retrieve a URL once. Never raises; every failure is a recorded status.

    A target already dead at capture is information, not an error (§6.6): 404
    and 410 record as `gone` rather than being retried indefinitely. Anything
    that might succeed later records as `unreachable`, and so does a target
    refused by `refuse_target` -- it lands in triage, where a human can paste
    the content if they meant it.
    """
    objection = refuse_target(target)
    if objection is not None:
        return Fetched(status="unreachable", error=f"refused: {objection}")

    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_LimitedRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(RAW_CAP + 1)
            landed = response.geturl()
            return Fetched(
                status="ok",
                http_status=response.status,
                content_type=response.headers.get("Content-Type"),
                body=body,
                final_url=landed if landed != target else None,
            )
    except urllib.error.HTTPError as exc:
        dead = exc.code in (404, 410)
        return Fetched(
            status="gone" if dead else "unreachable",
            http_status=exc.code,
            error=f"HTTP {exc.code} {exc.reason}",
        )
    except RefusedTarget as exc:
        return Fetched(status="unreachable", error=f"refused: {exc}")
    except Exception as exc:  # timeouts, DNS, TLS, malformed URLs, redirect loops
        return Fetched(status="unreachable", error=f"{type(exc).__name__}: {exc}")


class _LimitedRedirects(urllib.request.HTTPRedirectHandler):
    """Bounded in count, and re-checked at every hop.

    Checking only the original URL would be no check at all: a public URL that
    302s to 169.254.169.254 is the standard way around a naive filter, and the
    destination is exactly what gets fetched and stored.
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        objection = refuse_target(newurl)
        if objection is not None:
            raise RefusedTarget(f"redirect to {newurl[:80]} refused: {objection}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SnapshotStore:
    """The snapshot database. Opened lazily; absent means snapshots are off."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Version first, before any PRAGMA that writes -- the same rule
        # `Store.__init__` states and for the same reason: setting journal_mode
        # on a database that is about to be refused modifies the file, and a
        # refusal that edits the thing it refused is not a refusal.
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"snapshot store {self.path} is version {version}, newer than this "
                f"build's {SCHEMA_VERSION}. Upgrade seshat."
            )
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        if version == 0:
            with self.db:
                self.db.executescript(SCHEMA)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version < SCHEMA_VERSION:
            # Narrowly additive: new tables only, no column rewritten and no
            # row touched. That is a different and much safer class of change
            # than altering `snapshot`, which holds witnesses that cannot be
            # regenerated -- so it is allowed here and refused there.
            with self.db:
                self.db.executescript(ADDITIVE)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- queue

    def enqueue(
        self, note_id: str, targets: Iterable[tuple[str, str | None, str | None]]
    ) -> int:
        """Queue captures for a note. Never raises into the write path (§6.3).

        Each target carries its expectation (§6.9) so the capture can record
        what was sought as well as what was found.
        """
        added = 0
        with self._lock, self.db:
            for target, expectation, source in targets:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO fetch_queue(
                           note_id, target, enqueued_at, expectation, expectation_source)
                       VALUES (?, ?, ?, ?, ?)""",
                    (note_id, target, now(), expectation, source),
                )
                added += cur.rowcount or 0
        return added

    def pending(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT note_id, target FROM fetch_queue ORDER BY enqueued_at, target"
            ).fetchall()
        return [(r["note_id"], r["target"]) for r in rows]

    def expectation_for(self, note_id: str, target: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT expectation FROM fetch_queue WHERE note_id = ? AND target = ?",
                (note_id, target),
            ).fetchone()
        return row["expectation"] if row else None

    def record(self, note_id: str, target: str, result: Fetched) -> None:
        """Write the witness and drop the queue row, atomically.

        Snapshots are immutable: a row that already exists is never rewritten,
        so a re-queued capture cannot overwrite an earlier, better one.
        """
        stamp = now()
        with self._lock, self.db:
            existing = self.db.execute(
                "SELECT 1 FROM snapshot WHERE note_id = ? AND target = ?", (note_id, target)
            ).fetchone()
            expectation = self.db.execute(
                """SELECT expectation, expectation_source FROM fetch_queue
                   WHERE note_id = ? AND target = ?""",
                (note_id, target),
            ).fetchone()
            if existing is None:
                body = result.body or b""
                truncated = len(body) > RAW_CAP
                self.db.execute(
                    """INSERT INTO snapshot(note_id, target, captured_at, status,
                                            title, text, full_hash, full_length,
                                            raw_length, extraction, expectation,
                                            expectation_source, final_url,
                                            http_status, content_type)
                       VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)""",
                    (note_id, target, stamp, result.status,
                     len(body) if result.body else None,
                     expectation["expectation"] if expectation else None,
                     expectation["expectation_source"] if expectation else None,
                     result.final_url, result.http_status, result.content_type),
                )
                if result.body is not None:
                    self.db.execute(
                        """INSERT INTO raw(note_id, target, fetched_at, http_status,
                                           content_type, body, body_hash, body_length, truncated)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (note_id, target, stamp, result.http_status, result.content_type,
                         body[:RAW_CAP], hashlib.sha256(body).hexdigest(), len(body),
                         int(truncated)),
                    )
            self.db.execute(
                "DELETE FROM fetch_queue WHERE note_id = ? AND target = ?", (note_id, target)
            )

    def defer(self, note_id: str, target: str, error: str) -> None:
        """Leave a failed capture queued so it can be retried deliberately."""
        with self._lock, self.db:
            self.db.execute(
                """UPDATE fetch_queue SET attempts = attempts + 1, last_error = ?
                   WHERE note_id = ? AND target = ?""",
                (error, note_id, target),
            )

    # -------------------------------------------------------- extraction

    def needs_extraction(self, limit: int = 32) -> list[tuple[str, str, bytes, str | None]]:
        """Captures holding bytes that nothing has read yet.

        The backlog is a query, not a table: a snapshot with `extraction IS
        NULL` and a raw row present needs extracting. Re-extracting under a
        better extractor is therefore a targeted reset plus a drain, the same
        shape §6.3 uses for model migration.
        """
        with self._lock:
            rows = self.db.execute(
                """SELECT s.note_id, s.target, r.body, r.content_type
                   FROM snapshot s JOIN raw r
                     ON r.note_id = s.note_id AND r.target = s.target
                   WHERE s.extraction IS NULL
                   ORDER BY s.captured_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [(r["note_id"], r["target"], r["body"], r["content_type"]) for r in rows]

    def extract_one(
        self, note_id: str, target: str, body: bytes, content_type: str | None
    ) -> bool:
        """Read one capture's bytes and fill its derived columns.

        `thin` is set here and only here from a structural judgement (§6.6) --
        never from §6.9's expectation check, which is candidate generation for
        review and must not become a verdict.
        """
        from .extract import EXTRACTOR, FETCH_PREFIX, Extraction, extract, is_thin

        try:
            result = extract(body, content_type)
        except Exception:
            # `extract` is documented never to raise, so reaching here means it
            # was wrong about that. Record the failure ON THE ROW rather than
            # returning and leaving it for the next drain: the backlog is a
            # query over `extraction IS NULL`, so an unmarked failure is
            # re-selected forever and every capture behind it waits (§6.6).
            #
            # Marked `failed:`, it leaves the backlog, becomes visible in the
            # triage queue, and is still re-run by `reset_extraction` when a
            # better extractor arrives -- the same recovery path every machine
            # reading has. `thin` because that is exactly what this is: bytes
            # held, no readable text, and a human who can paste it while the
            # target may still be live.
            log.exception("extraction failed for %s %s", note_id, target)
            return self.store_extraction(
                note_id, target,
                Extraction(title=None, text="", full_length=0,
                           extractor=f"{FAILED_PREFIX}{EXTRACTOR}"),
                status="thin",
            )
        result.extractor = f"{FETCH_PREFIX}{result.extractor}"
        thin = is_thin(result.full_length, len(body), result.unsupported)
        return self.store_extraction(
            note_id, target, result, status="thin" if thin else None
        )

    def drain_extraction(self, limit: int = 1000) -> int:
        """Extract everything holding unread bytes.

        One capture that cannot be read must not hold up the rest. The deadline
        is on *detection* (§3.6): every row behind a stalled one is a recovery
        window closing unwatched, so a row that cannot be processed is marked
        and stepped over, never returned on.
        """
        done = 0
        while done < limit:
            batch = self.needs_extraction(min(32, limit - done))
            if not batch:
                break
            progressed = False
            for note_id, target, body, content_type in batch:
                if self.extract_one(note_id, target, body, content_type):
                    done += 1
                    progressed = True
            if not progressed:
                # Nothing in a full batch could be marked either way. Returning
                # beats spinning: the batch query would hand back the same rows.
                break
        return done

    def extraction_backlog(self) -> int:
        with self._lock:
            return self.db.execute(
                """SELECT COUNT(*) FROM snapshot s JOIN raw r
                     ON r.note_id = s.note_id AND r.target = s.target
                   WHERE s.extraction IS NULL"""
            ).fetchone()[0]

    def store_extraction(
        self, note_id: str, target: str, extraction, status: str | None = None
    ) -> bool:
        """Fill a snapshot's derived columns. Returns whether anything changed.

        §10.3: a snapshot with no content is being **filled**, not rewritten;
        one that already holds content is immutable like the note it witnesses.
        The `extraction IS NULL` clause is the whole guarantee -- it is what
        stops a re-run quietly replacing a human's pasted text with a parser's
        worse attempt.
        """
        import hashlib as _hashlib

        with self._lock, self.db:
            cursor = self.db.execute(
                """UPDATE snapshot
                   SET title = ?, text = ?, full_hash = ?, full_length = ?,
                       extraction = ?, status = COALESCE(?, status)
                   WHERE note_id = ? AND target = ? AND extraction IS NULL""",
                (
                    extraction.title,
                    extraction.text,
                    _hashlib.sha256(extraction.text.encode("utf-8")).hexdigest(),
                    extraction.full_length,
                    extraction.extractor,
                    status,
                    note_id,
                    target,
                ),
            )
            return bool(cursor.rowcount)

    def reset_extraction(self, extractor: str | None = None) -> int:
        """Clear machine extractions so the worker re-reads stored bytes.

        Only the derived columns, and never manual ones: pasted content (§10.2)
        is a witness nothing can regenerate, so it is excluded by the query
        rather than protected by convention.
        """
        with self._lock, self.db:
            params: list = [MANUAL + "%"]
            clause = "extraction IS NOT NULL AND extraction NOT LIKE ?"
            if extractor:
                clause += " AND extraction = ?"
                params.append(extractor)
            cursor = self.db.execute(
                f"""UPDATE snapshot
                    SET extraction = NULL, title = NULL, text = NULL,
                        full_hash = NULL, full_length = NULL,
                        status = CASE WHEN status = 'thin' THEN 'ok' ELSE status END
                    WHERE {clause}
                      AND EXISTS (SELECT 1 FROM raw r
                                  WHERE r.note_id = snapshot.note_id
                                    AND r.target = snapshot.target)""",
                params,
            )
            return cursor.rowcount

    def _clear_machine_reading(self, note_id: str, target: str) -> None:
        """Make room for a human-supplied reading, without losing anything.

        The rule, in one place so it cannot drift between the paste path and
        the extension path: **a reading is replaceable exactly when it can be
        regenerated**, and only a `fetch:` reading can. `raw` holds the bytes it
        was read from, those bytes are never touched, and re-running the
        extractor reproduces it exactly.

        A `failed:` marker is replaceable for the same reason and more obviously:
        it holds no reading at all, only the record that the extractor could not
        produce one, and it is the case a human is most likely to be repairing.

        Nothing human-supplied is replaceable. A pasted reading arrived by hand
        and nothing in the store can reproduce it. A browser reading looks
        regenerable but is not, in the case that matters: when it repairs an
        earlier fetch, `raw` still holds the *fetcher's* bytes -- the paywall
        interstitial -- because those are the original witness and are not
        overwritten. Regenerating from them would give back the paywall, not
        the reading. Treating browser readings as replaceable would have been a
        rule that is true in the fresh-capture case and quietly false in the
        repair case.

        The cost is that a careless paste cannot be corrected through this
        path. That is the conservative failure, and the right one for a table
        whose whole purpose is to hold evidence.
        """
        self.db.execute(
            """UPDATE snapshot SET extraction = NULL
               WHERE note_id = ? AND target = ?
                 AND (extraction IS NULL OR extraction LIKE ? OR extraction LIKE ?)""",
            (note_id, target, FETCH + ":%", FAILED_PREFIX + "%"),
        )

    def record_browser(
        self,
        note_id: str,
        target: str,
        text: str,
        html: bytes | None = None,
        title: str | None = None,
        expectation: str | None = None,
    ) -> bool:
        """Attach a browser capture as evidence for a note (§10.2, §6.10).

        **This path has no fetch deadline at all.** §6.6's deadline exists
        because of the gap between reading a page and fetching it afterwards,
        which is where rot and drift live. The browser supplies content at write
        time, so the gap is zero: nothing is queued, `pending` never rises,
        `thin` is decidable immediately, and §10.2's recovery window never opens.

        The DOM is kept as `raw` where supplied, so extraction stays re-runnable
        exactly as it is for a fetch -- but note the provenance difference §10.2
        draws: a rendered DOM is better fidelity *and* rendered for that viewer,
        personalisation and A/B bucketing included.
        """
        from .extract import BROWSER_PREFIX, TEXT_CAP, Extraction, is_thin

        body = text.strip()
        extraction = Extraction(
            title=title,
            text=body[:TEXT_CAP],
            full_length=len(body),
            extractor=f"{BROWSER_PREFIX}dom/1",
        )
        raw_length = len(html) if html else len(body.encode("utf-8"))
        thin = is_thin(len(body), raw_length)
        stamp = now()
        with self._lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO snapshot(
                       note_id, target, captured_at, status, raw_length, expectation,
                       expectation_source, http_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                # The source describes an expectation, so it cannot outlive one:
                # a row claiming `anchor` with a null expectation says a label
                # was read that never existed.
                (note_id, target, stamp, "thin" if thin else "ok", raw_length,
                 expectation, "anchor" if expectation else None),
            )
            if html:
                self.db.execute(
                    """INSERT OR IGNORE INTO raw(note_id, target, fetched_at, http_status,
                                                 content_type, body, body_hash, body_length,
                                                 truncated)
                       VALUES (?, ?, ?, NULL, 'text/html', ?, ?, ?, ?)""",
                    (note_id, target, stamp, html[:RAW_CAP],
                     hashlib.sha256(html).hexdigest(), len(html), int(len(html) > RAW_CAP)),
                )
            # Nothing to fetch: the content is already here.
            self.db.execute(
                "DELETE FROM fetch_queue WHERE note_id = ? AND target = ?", (note_id, target)
            )
            self._clear_machine_reading(note_id, target)
        return self.store_extraction(
            note_id, target, extraction, status="thin" if thin else "ok"
        )

    def paste(self, note_id: str, target: str, text: str, title: str | None = None) -> bool:
        """Record human-supplied content for a capture (§10.2).

        Clears the whole paywall/SSO/SPA class of failure without seshat ever
        handling a credential: the user is already authenticated and already
        looking at the page.

        §10.3 says a snapshot holding content is immutable, and the paywall case
        sits exactly on that line: the interstitial IS content, and a truthful
        record of what the URL served an anonymous fetcher. The resolution is
        that **`raw` is never touched**. The bytes the fetcher received stay
        exactly where they were, so nothing is destroyed; only the derived
        reading is replaced, by a better one, with `extraction='manual'` saying
        who read it. Both witnesses survive, which is what the spec is
        protecting.

        A previous *manual* paste is never replaced. Pasted text cannot be
        regenerated from anything, so overwriting it would be the real loss
        §10.3 forbids.
        """
        from .extract import MANUAL as MANUAL_MARK
        from .extract import TEXT_CAP, Extraction

        body = text.strip()
        extraction = Extraction(
            title=title, text=body[:TEXT_CAP], full_length=len(body), extractor=MANUAL_MARK
        )
        with self._lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO snapshot(note_id, target, captured_at, status)
                   VALUES (?, ?, ?, 'ok')""",
                (note_id, target, now()),
            )
            self._clear_machine_reading(note_id, target)
        return self.store_extraction(note_id, target, extraction, status="ok")

    # ------------------------------------------------------------ reading

    def sources_for(self, note_id: str) -> list[dict]:
        """Snapshots belonging to one note, for `read(with_sources=True)` (§6.6)."""
        with self._lock:
            rows = self.db.execute(
                """SELECT s.target, s.captured_at, s.status, s.title, s.text,
                          s.full_hash, s.full_length, s.raw_length, s.extraction,
                          s.expectation, s.expectation_source, s.final_url,
                          s.http_status, s.content_type,
                          r.body_hash, r.body_length, r.truncated
                   FROM snapshot s LEFT JOIN raw r
                     ON r.note_id = s.note_id AND r.target = s.target
                   WHERE s.note_id = ? ORDER BY s.target""",
                (note_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            # Say plainly that bytes are held but not yet parsed, rather than
            # returning a null `text` that reads as "nothing was captured".
            item["extracted"] = item["extraction"] is not None
            item["provenance"] = (item["extraction"] or "").split(":")[0] or None
            # §6.9: computed on demand, never stored and never a verdict.
            item["expectation_met"] = expectation_met(item["expectation"], item["text"])
            out.append(item)
        return out

    # ------------------------------------------------------------- triage

    def acknowledge(self, note_id: str, target: str, reason: str | None = None) -> None:
        """Record that a target is irreducibly unpreservable (§10.4).

        The note remains valid. It cites a source nobody can check, which is a
        property of the snapshot and weaker evidence -- not a defect in the
        note, and never grounds for superseding it.
        """
        with self._lock, self.db:
            self.db.execute(
                """INSERT INTO acknowledgement(note_id, target, acknowledged_at, reason)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(note_id, target) DO UPDATE SET
                     acknowledged_at = excluded.acknowledged_at, reason = excluded.reason""",
                (note_id, target, now(), reason),
            )
            self.db.execute(
                "DELETE FROM fetch_queue WHERE note_id = ? AND target = ?", (note_id, target)
            )

    def acknowledged(self) -> set[tuple[str, str]]:
        with self._lock:
            return {
                (r["note_id"], r["target"])
                for r in self.db.execute("SELECT note_id, target FROM acknowledgement")
            }

    def retry(self, note_id: str, target: str) -> bool:
        """Re-queue a transient failure (§10.2). Returns whether it was queued.

        Only failures, and only failures that retrieved nothing. A successful
        capture is immutable and re-fetching it would be pointless; a failed one
        that nonetheless holds bytes has a witness, and those bytes are the only
        data in the system nothing can regenerate.
        """
        with self._lock, self.db:
            row = self.db.execute(
                """SELECT status, expectation, expectation_source FROM snapshot
                   WHERE note_id = ? AND target = ?""",
                (note_id, target),
            ).fetchone()
            if row is None or row["status"] == "ok":
                return False
            held = self.db.execute(
                "SELECT 1 FROM raw WHERE note_id = ? AND target = ?", (note_id, target)
            ).fetchone()
            if held is not None:
                # Bytes were retrieved, so this capture HAS a witness -- a
                # paywall interstitial is still a truthful record of what the
                # URL served an anonymous fetcher, and `paste()` is built around
                # never touching it. Re-fetching would delete it and might come
                # back with nothing, trading the only unrecoverable data in the
                # system for a second attempt. Repair by pasting instead.
                #
                # Unreachable from the review UI today, which only offers retry
                # for `unreachable` and those hold no bytes. Guarded here rather
                # than relying on which button gets rendered.
                log.info(
                    "refusing to re-queue %s %s: %d bytes already captured",
                    note_id, target,
                    self.db.execute(
                        "SELECT body_length FROM raw WHERE note_id = ? AND target = ?",
                        (note_id, target),
                    ).fetchone()[0],
                )
                return False
            self.db.execute(
                "DELETE FROM snapshot WHERE note_id = ? AND target = ?", (note_id, target)
            )
            self.db.execute(
                """INSERT OR REPLACE INTO fetch_queue(
                       note_id, target, enqueued_at, attempts, expectation, expectation_source)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (note_id, target, now(), row["expectation"], row["expectation_source"]),
            )
        return True

    def failures(self) -> list[dict]:
        """The triage queue (§10.2): captures needing a human, worst first.

        Acknowledged targets are excluded -- that is what acknowledging is for.
        """
        acknowledged = self.acknowledged()
        with self._lock:
            rows = self.db.execute(
                """SELECT note_id, target, status, captured_at, expectation,
                          expectation_source, text, full_length, final_url, http_status
                   FROM snapshot WHERE status != 'ok' ORDER BY captured_at DESC"""
            ).fetchall()
        order = {"thin": 0, "unreachable": 1, "gone": 2}
        out = [dict(r) for r in rows if (r["note_id"], r["target"]) not in acknowledged]
        out.sort(key=lambda r: (order.get(r["status"], 9), r["captured_at"]))
        return out

    def unmet_expectations(self) -> list[dict]:
        """Captures that succeeded structurally but may be the wrong page (§6.9).

        Candidates for review, never verdicts.
        """
        acknowledged = self.acknowledged()
        with self._lock:
            rows = self.db.execute(
                """SELECT note_id, target, expectation, expectation_source, text, full_length
                   FROM snapshot
                   WHERE status = 'ok' AND expectation IS NOT NULL AND text IS NOT NULL"""
            ).fetchall()
        return [
            dict(r) for r in rows
            if (r["note_id"], r["target"]) not in acknowledged
            and expectation_met(r["expectation"], r["text"]) is False
        ]

    def histogram(self) -> dict[str, int]:
        """§3.6: a histogram, not a counter.

        A permanently failed fetch has already left the queue, so a bare
        backlog count reads zero while the data is missing -- the exact silent
        failure the preservation model exists to prevent.
        """
        with self._lock:
            counts = dict(
                self.db.execute("SELECT status, COUNT(*) FROM snapshot GROUP BY status").fetchall()
            )
            pending = self.db.execute("SELECT COUNT(*) FROM fetch_queue").fetchone()[0]
            unextracted = self.db.execute(
                """SELECT COUNT(*) FROM snapshot s JOIN raw r
                     ON r.note_id = s.note_id AND r.target = s.target
                   WHERE s.extraction IS NULL"""
            ).fetchone()[0]
        result = {"pending": pending}
        result.update({status: counts.get(status, 0) for status in STATUSES})
        # Not in the spec's minimum shape: this build holds bytes it has not
        # parsed, and reporting those as plain `ok` would overstate what exists.
        result["unextracted"] = unextracted
        with self._lock:
            result["acknowledged"] = self.db.execute(
                "SELECT COUNT(*) FROM acknowledgement"
            ).fetchone()[0]
        return result


class SnapshotWorker:
    """Drains the capture queue off the write path (§6.3, §6.6).

    A separate queue from embedding, and for a stated reason: embedding is
    local, fast and fails atomically, while fetching is network-bound, slow and
    hangs. One stalled request must not block the embedding backlog.
    """

    def __init__(
        self,
        snapshots: SnapshotStore,
        fetcher: Callable[[str], Fetched] = http_fetch,
        poll: float = 1.0,
        max_attempts: int = 3,
        extract_now: bool = True,
    ):
        self.snapshots = snapshots
        self.extract_now = extract_now
        self.fetcher = fetcher
        self.poll = poll
        self.max_attempts = max_attempts
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="seshat-snapshots", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # a worker crash must not take the server with it
                log.exception("snapshot worker iteration failed")
            self._wake.wait(self.poll)
            self._wake.clear()

    def run_once(self) -> int:
        """Attempt every queued capture once. Returns how many were recorded.

        Synchronous and deterministic, so tests drive it directly instead of
        racing a thread.
        """
        done = 0
        for note_id, target in self.snapshots.pending():
            if self._stop.is_set():
                break
            try:
                result = self.fetcher(target)
            except Exception as exc:  # a fetcher must not be able to kill the queue
                result = Fetched(status="unreachable", error=f"{type(exc).__name__}: {exc}")
            if result.status == "unreachable" and self._retriable(note_id, target):
                self.snapshots.defer(note_id, target, result.error or "unreachable")
                continue
            self.snapshots.record(note_id, target, result)
            if self.extract_now and result.body is not None:
                # Immediately, not later: the deadline is on *detection* (§3.6).
                # A thin capture can only be repaired while the page is live.
                self.snapshots.extract_one(note_id, target, result.body, result.content_type)
            done += 1
        return done

    def _retriable(self, note_id: str, target: str) -> bool:
        row = self.snapshots.db.execute(
            "SELECT attempts FROM fetch_queue WHERE note_id = ? AND target = ?", (note_id, target)
        ).fetchone()
        return row is not None and row["attempts"] + 1 < self.max_attempts
