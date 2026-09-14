"""Snapshots: what a target said when a given note was written. Spec §6.6.

A snapshot is a witness, keyed `(note_id, target)` and immutable exactly like
the note it belongs to. Superseding a note leaves its witness intact, because
the old belief was formed against the old page.

**This is the only unrecoverable data in the system.** `link` is regenerable
from note text in seconds; a snapshot is gone forever if dropped, and the two
share a primary key. They live in SEPARATE DATABASE FILES so that a rebuild of
the derived index cannot reach this table even by accident. See
`Store._rebuild_links`.

Increment-one form: **capture raw bytes and hash them, extract nothing.**
Extraction is re-runnable over stored bytes; an unfetched page is not. Notes
written before a fetcher exists have permanently unrecoverable links, so
capture ships before extraction does, deliberately in that order.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("seshat.snapshots")

SCHEMA_VERSION = 1

RAW_CAP = 8 * 1024 * 1024
"""Bytes retained per capture. Generous: this is the copy extraction re-runs
over, and a truncated body is a permanently degraded witness."""

FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
USER_AGENT = "seshat/0.1 (+https://github.com/; personal note store; capture-once)"

STATUSES = ("ok", "gone", "unreachable", "thin")
"""§6.6. `thin` means extraction yielded implausibly little and needs human
attention while manual recovery is still possible -- it cannot be produced
until an extractor exists, so this build never emits it."""

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
  extraction    TEXT,             -- extractor name/version; NULL = none run yet
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

-- Durable, so a crash between write and fetch does not lose a capture that
-- can never be taken again.
CREATE TABLE fetch_queue (
  note_id      TEXT NOT NULL,
  target       TEXT NOT NULL,
  enqueued_at  TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  PRIMARY KEY (note_id, target)
);
"""


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


def snapshot_path_for(notes_db: str | Path) -> str:
    """Snapshots live outside the notes store, which stays small and portable."""
    text = str(notes_db)
    if text == ":memory:":
        return ":memory:"
    path = Path(text)
    return str(path.with_name(path.name + ".snapshots.db"))


def http_fetch(target: str, timeout: float = FETCH_TIMEOUT) -> Fetched:
    """Retrieve a URL once. Never raises; every failure is a recorded status.

    A target already dead at capture is information, not an error (§6.6): 404
    and 410 record as `gone` rather than being retried indefinitely. Anything
    that might succeed later records as `unreachable`.
    """
    if not target.lower().startswith(("http://", "https://")):
        return Fetched(status="unreachable", error=f"unsupported scheme: {target[:40]}")

    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_LimitedRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(RAW_CAP + 1)
            return Fetched(
                status="ok",
                http_status=response.status,
                content_type=response.headers.get("Content-Type"),
                body=body,
            )
    except urllib.error.HTTPError as exc:
        dead = exc.code in (404, 410)
        return Fetched(
            status="gone" if dead else "unreachable",
            http_status=exc.code,
            error=f"HTTP {exc.code} {exc.reason}",
        )
    except Exception as exc:  # timeouts, DNS, TLS, malformed URLs, redirect loops
        return Fetched(status="unreachable", error=f"{type(exc).__name__}: {exc}")


class _LimitedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS


class SnapshotStore:
    """The snapshot database. Opened lazily; absent means snapshots are off."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.db:
                self.db.executescript(SCHEMA)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise RuntimeError(
                f"snapshot store {self.path} is version {version}, build speaks {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- queue

    def enqueue(self, note_id: str, targets: Iterable[str]) -> int:
        """Queue captures for a note. Never raises into the write path (§6.3)."""
        added = 0
        with self._lock, self.db:
            for target in targets:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO fetch_queue(note_id, target, enqueued_at)
                       VALUES (?, ?, ?)""",
                    (note_id, target, now()),
                )
                added += cur.rowcount or 0
        return added

    def pending(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT note_id, target FROM fetch_queue ORDER BY enqueued_at, target"
            ).fetchall()
        return [(r["note_id"], r["target"]) for r in rows]

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
            if existing is None:
                body = result.body or b""
                truncated = len(body) > RAW_CAP
                self.db.execute(
                    """INSERT INTO snapshot(note_id, target, captured_at, status,
                                            title, text, full_hash, full_length,
                                            raw_length, extraction)
                       VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL)""",
                    (note_id, target, stamp, result.status, len(body) if result.body else None),
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

    # ------------------------------------------------------------ reading

    def sources_for(self, note_id: str) -> list[dict]:
        """Snapshots belonging to one note, for `read(with_sources=True)` (§6.6)."""
        with self._lock:
            rows = self.db.execute(
                """SELECT s.target, s.captured_at, s.status, s.title, s.text,
                          s.full_hash, s.full_length, s.raw_length, s.extraction,
                          r.body_hash, r.body_length, r.content_type, r.truncated
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
            out.append(item)
        return out

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
                "SELECT COUNT(*) FROM snapshot WHERE extraction IS NULL AND status = 'ok'"
            ).fetchone()[0]
        result = {"pending": pending}
        result.update({status: counts.get(status, 0) for status in STATUSES})
        # Not in the spec's minimum shape: this build holds bytes it has not
        # parsed, and reporting those as plain `ok` would overstate what exists.
        result["unextracted"] = unextracted
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
    ):
        self.snapshots = snapshots
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
            done += 1
        return done

    def _retriable(self, note_id: str, target: str) -> bool:
        row = self.snapshots.db.execute(
            "SELECT attempts FROM fetch_queue WHERE note_id = ? AND target = ?", (note_id, target)
        ).fetchone()
        return row is not None and row["attempts"] + 1 < self.max_attempts
