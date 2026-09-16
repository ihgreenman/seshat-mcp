"""SQLite storage and retrieval for seshat. Spec §5, §6.

Retrieval is hybrid when it can be: FTS5/BM25 and vector KNN run independently
and fuse with RRF (§6). Every part of the vector side is optional at runtime --
no sqlite-vec, no Ollama, or simply nothing embedded yet all degrade `context`
to the keyword ranking alone rather than failing it (§6.3).
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from . import embeddings, extract as extract_mod, ids, links, vectors
from .embeddings import DEFAULT_DIM

if TYPE_CHECKING:  # pragma: no cover
    from .embeddings import Embedder
    from .snapshots import SnapshotStore

log = logging.getLogger("seshat.store")

DEFAULT_THETA = 0.8
"""Pool-membership threshold (§5.1). Open question §9.2 -- no principled basis."""

RRF_K = 60
"""Reciprocal rank fusion constant (§6).

**Fixed, not a tuning parameter.** Measured invariant from k=5 to k=300 --
identical MRR across a 60x range -- so there is nothing here to tune. Spec 1.2
removed the corresponding open question."""

VECTOR_COOLDOWN = 30.0
"""Seconds to skip the vector side after it fails. Without this, every
`context` call pays the embedder timeout while Ollama is down -- turning a
graceful degradation into an unusable one."""

DEFAULT_LIMIT = 20
DEFAULT_EDGE_LIMIT = 10

SCHEMA_VERSION = 4
"""On-disk schema version -- `store_version` in §11.1, independent of both the
spec version and the software version.

Version 4 consolidates 1..3 into one CREATE. Spec 1.3 needed a column on a
table that was not yet populated anywhere, so a one-time reset was authorised
in place of a migration chain -- the cheapest such break the design will get.
There is deliberately no path from an older store: opening one is refused with
instructions rather than silently upgraded or silently destroyed.
"""

SCHEMA = """
-- Store identity. Written at creation, bumped only on migration. §11.2
CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

CREATE TABLE note (
  id          TEXT PRIMARY KEY,   -- four BIP-39 words, hyphenated. §6.1
  desc        TEXT NOT NULL,
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX note_created ON note(created_at);   -- ids are not time-sortable

-- An edge is (old_id, new_id); its score is the latest assessment. §5.5
CREATE TABLE assessment (
  old_id       TEXT NOT NULL REFERENCES note(id),
  new_id       TEXT NOT NULL REFERENCES note(id),
  retained     REAL NOT NULL CHECK (retained BETWEEN 0 AND 1),
  rationale    TEXT,            -- why this score; embedded, not a note ref
  asserted_at  TEXT NOT NULL,
  PRIMARY KEY (old_id, new_id, asserted_at)
);
CREATE INDEX assessment_new ON assessment(new_id);

CREATE VIEW supersession AS
  SELECT old_id, new_id, retained, rationale, asserted_at
  FROM assessment a
  WHERE asserted_at = (
    SELECT MAX(asserted_at) FROM assessment b
    WHERE b.old_id = a.old_id AND b.new_id = a.new_id
  );

-- §5.1. LEFT JOIN, not JOIN: a note with no outgoing edges retains 1.0, and an
-- inner join would drop every never-superseded note -- i.e. most of the store.
CREATE VIEW pool_retained AS
  SELECT n.id AS id, COALESCE(MAX(s.retained), 1.0) AS retained
  FROM note n LEFT JOIN supersession s ON s.old_id = n.id
  GROUP BY n.id;

CREATE VIRTUAL TABLE note_fts USING fts5(desc, text, content='note', content_rowid='rowid');

-- Notes are immutable (§2), so an insert trigger is the whole sync story.
CREATE TRIGGER note_ai AFTER INSERT ON note BEGIN
  INSERT INTO note_fts(rowid, desc, text) VALUES (new.rowid, new.desc, new.text);
END;

-- Rationales are findable when looked for, and never compete with notes in
-- `context`: separate index, deliberately not reachable from retrieval. §5.5
CREATE VIRTUAL TABLE assessment_fts USING fts5(rationale, content='');
CREATE TRIGGER assessment_ai AFTER INSERT ON assessment WHEN new.rationale IS NOT NULL BEGIN
  INSERT INTO assessment_fts(rowid, rationale) VALUES (new.rowid, new.rationale);
END;

-- Derived index over note text. Rebuildable, disposable. §6.5
-- Shares a key with `snapshot`, which is neither -- see snapshots.py.
CREATE TABLE link (
  note_id  TEXT NOT NULL REFERENCES note(id),
  kind     TEXT NOT NULL,         -- 'url' | 'hash' | 'note'
  target   TEXT NOT NULL,
  label    TEXT,                  -- markdown anchor text; §6.9's expectation
  PRIMARY KEY (note_id, kind, target)
);
CREATE INDEX link_target ON link(target);   -- backlinks and DISTINCT target

-- Embedding provenance. A row's absence, or model != current, means
-- "needs embedding". Model migration is a DELETE plus a worker drain. §6.3
-- `note_vec` is NOT created here: a vec0 table in the schema would make the
-- store unopenable without the extension. See vectors.ensure_table.
CREATE TABLE embedding_meta (
  note_id     TEXT PRIMARY KEY REFERENCES note(id),
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  normalized  INTEGER NOT NULL,
  embedded_at TEXT NOT NULL
);

-- Lookups that missed. §6.1
CREATE TABLE near_miss (
  requested    TEXT NOT NULL,
  candidate    TEXT REFERENCES note(id),  -- NULL = nothing within distance
  distance     INTEGER,
  first_seen   TEXT NOT NULL,
  hit_count    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (requested)
);
"""


class StoreVersionError(Exception):
    """The database on disk is not a schema this build can open."""


class SeshatError(Exception):
    """Base for errors the tool surface reports back to a caller."""


class NoteNotFound(SeshatError):
    """No note, and nothing within the recovery radius: a fabricated id (§6.1)."""


class AmbiguousId(SeshatError):
    """Several known notes sit at the same distance. Resolving would be a guess."""

    def __init__(self, requested: str, candidates: Sequence[str], distance: int):
        self.requested = requested
        self.candidates = list(candidates)
        self.distance = distance
        super().__init__(
            f"{requested!r} is {distance} word-edit(s) from {len(candidates)} known "
            f"notes ({', '.join(candidates)}); refusing to guess. Use context() instead."
        )


class CycleError(SeshatError):
    """The edge would make the supersession graph cyclic (§3.4)."""


def now() -> str:
    """UTC, microsecond, fixed width -- so string MAX() is chronological (§5.5)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Hit:
    """One retrieval result (§3.2).

    `score` is a SORT KEY, not a confidence. RRF is derived from position
    alone, so a perfect match at rank 1 and a worthless single-token match at
    rank 1 score identically -- and the value is invariant across a 60x range
    of k (§6.8), which is what a number encoding almost nothing looks like.
    `vector_similarity` is the field that can actually be low.

    On a recency listing all three are **null, never zero** (§3.2): no ranking
    was fused and no query vector exists, so they are undefined rather than
    low. Zero would read as "nothing matched" when nothing was asked.
    """

    id: str
    desc: str
    score: float | None
    vector_similarity: float | None = None
    matched: tuple[str, ...] | None = None


@dataclass
class Record:
    id: str
    desc: str
    text: str
    created_at: str
    supersedes: list[dict[str, Any]] = field(default_factory=list)
    supersedes_total: int = 0
    superseded_by: list[dict[str, Any]] = field(default_factory=list)
    superseded_by_total: int = 0
    heads: list[str] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    backlinks: list[dict[str, Any]] = field(default_factory=list)
    backlinks_total: int = 0
    sources: list[dict[str, Any]] | None = None
    resolved_by_proximity: dict[str, Any] | None = None


def _rrf(rank: int) -> float:
    """Reciprocal rank fusion term, 1-indexed rank (§6).

    With one retriever this is a monotone relabelling of the rank, which is the
    point: the score a caller sees keeps the same shape and scale when the
    vector side is added, so triage habits built now stay calibrated.
    """
    return 1.0 / (RRF_K + rank)


def fuse(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal rank fusion over independent rankings (§6).

        score(d) = sum over rankings of 1 / (k + rank(d))

    Rank-based, so BM25 scores and cosine distances never have to be reconciled
    -- which matters because they are not on comparable scales and any attempt
    to normalise them would be an invented calibration.

    A document found by both retrievers outscores one found by either alone,
    which is the entire point of running them separately.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, note_id in enumerate(ranking, start=1):
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + rank)
    return scores


_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+")

STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to from by
for with without about into over under again further is are was were be been
being am do does did doing have has had having i you he she it we they them
his her its our your their what which who whom when where why how all any both
each few more most other some such no nor not only own same so too very can
will just should now as up down out off above below here there one two three
""".split())
"""Function words dropped from FTS expansion.

Not tuning: with `OR` expansion every token can single-handedly retrieve a
document, and RRF then treats that ranking as authoritative because it is
rank-based and cannot see how weak the match was. Observed live -- the query
"combining two rankings" matched a note on catastrophic cancellation, solely via
"two", and fusion promoted it above the note the vector side had correctly
ranked first. Function words carry no topical signal, so the cost of dropping
them is nil and the cost of keeping them is a spurious rank-1 hit.
"""


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is quoted, so no caller string can reach FTS5's query syntax.
    OR rather than the default AND: retrieval wants recall with bm25 doing the
    ranking, not a conjunctive filter that returns nothing on a five-word query.
    """
    tokens = _FTS_TOKEN.findall(text)
    if not tokens:
        return None
    content = [t for t in tokens if t.lower() not in STOPWORDS]
    # A query made entirely of function words is still a query -- searching for
    # "one two three" should find the note that says it.
    return " OR ".join(f'"{t}"' for t in (content or tokens))


def synchronized(method):
    """Hold the store lock for the whole operation, not per statement.

    The MCP SDK runs sync tool bodies in a worker thread pool, so a connection
    made on the main thread is touched from several others. One connection plus
    one lock is the right trade for a single-user local store -- and the lock
    must span an operation, since `resolve` followed by an INSERT is only
    atomic if nothing intervenes. A per-thread connection pool would not work:
    an in-memory store would give each thread its own empty database.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(
        self,
        path: str | Path,
        theta: float = DEFAULT_THETA,
        snapshots: "SnapshotStore | None" = None,
        embedder: "Embedder | None" = None,
        read_only: bool = False,
    ):
        self.path = str(path)
        self.read_only = read_only
        self.theta = theta
        self.snapshots = snapshots
        self.embedder = embedder
        self._lock = threading.RLock()
        self._on_captures_queued: Any = None
        self._on_note_written: Any = None
        self._vector_cooldown_until = 0.0
        if self.path != ":memory:" and not read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            # §10.3: the review interface must not permit editing a note. Making
            # its connection physically read-only means the absence is not a
            # discipline someone can reasonably relax later -- SQLite refuses.
            if self.path != ":memory:" and not Path(self.path).exists():
                raise StoreVersionError(f"{self.path} does not exist")
            self.db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Version first, before any PRAGMA that writes. Setting journal_mode on
        # a store that is about to be refused modifies the file -- a refusal
        # that edits the thing it refused is not a refusal.
        self._check_version()
        self.db.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
        # Optional and pre-v1, so its absence must cost nothing but quality.
        self.vector_loaded = vectors.load_extension(self.db)
        if not read_only:
            self._ensure_schema()
        if self.vector_loaded and not read_only:
            dim = embedder.dim if embedder else DEFAULT_DIM
            with self.db:
                vectors.ensure_table(self.db, dim)
            existing = vectors.stored_dim(self.db)
            if existing is not None and existing != dim:
                # Refuse rather than write vectors of the wrong width into a
                # table that will silently never match them.
                log.warning(
                    "note_vec holds dim %d but the embedder produces %d; "
                    "vector search disabled. Re-embed with `seshat reembed`.",
                    existing, dim,
                )
                self.vector_loaded = False

    @property
    def vector_ready(self) -> bool:
        """Whether the vector side can be consulted right now (§6.3)."""
        return self.vector_loaded and time.monotonic() >= self._vector_cooldown_until

    def close(self) -> None:
        self.db.close()

    def _check_version(self) -> None:
        """Refuse an unopenable store before touching a single byte of it."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version in (0, SCHEMA_VERSION):
            return
        older = version < SCHEMA_VERSION
        raise StoreVersionError(
            f"{self.path} is schema version {version}, "
            f"{'older' if older else 'newer'} than this build's {SCHEMA_VERSION}.\n"
            + (
                "Schema 4 consolidated versions 1-3 and there is no migration path: "
                "spec 1.3 needed a column on a table nothing had populated, and a "
                "one-time reset was taken instead of a migration chain.\n"
                "Run `seshat reset` to archive this store and start a new one, or "
                "point --db at a different file."
                if older
                else "Upgrade seshat rather than downgrading the store."
            )
        )

    def _ensure_schema(self) -> None:
        """Create the schema, or refuse a database this build cannot open.

        There is no migration path into version 4. Spec 1.3 required a column on
        a table nothing had populated yet, so a one-time reset was authorised
        instead of a migration chain. An older store is therefore refused with
        instructions -- never upgraded on a guess, and never deleted on the
        store's own initiative, because the only thing worse than an unopenable
        note store is one that opens empty.
        """
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        with self.db:
            if version == 0:
                self.db.executescript(SCHEMA)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._write_meta(fresh=True)
            else:
                self._write_meta(fresh=False)

    def _write_meta(self, fresh: bool) -> None:
        from . import SPEC_VERSION

        rows = {"store_version": str(SCHEMA_VERSION)}
        if fresh:
            rows["created_under_spec"] = SPEC_VERSION
            rows["created_at"] = now()
        for key, value in rows.items():
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def meta(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM meta"))

    # ---------------------------------------------------------------- ids

    def _exists(self, note_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM note WHERE id = ?", (note_id,)).fetchone() is not None

    @synchronized
    def mint_id(self) -> str:
        while True:
            candidate = ids.new_id()
            if not self._exists(candidate):
                return candidate

    @synchronized
    def resolve(self, raw: str) -> ids.Resolution:
        """Lenient lookup, never silent substitution (§6.1).

        Raises AmbiguousId or NoteNotFound rather than picking; both outcomes
        are recorded in `near_miss`, which is what separates a corrupted id from
        a fabricated one.
        """
        canonical = ids.canonicalize(raw)
        if canonical is not None and self._exists(canonical):
            return ids.Resolution(id=canonical, requested=raw)

        key = "-".join(ids.split(raw))
        cached = self.db.execute(
            "SELECT candidate, distance FROM near_miss WHERE requested = ?", (key,)
        ).fetchone()
        if cached is not None and cached["candidate"] and self._exists(cached["candidate"]):
            self._bump_near_miss(key, cached["candidate"], cached["distance"])
            return ids.Resolution(
                id=cached["candidate"], requested=raw, by_proximity=True,
                distance=cached["distance"] or 0,
            )

        known = (row[0] for row in self.db.execute("SELECT id FROM note"))
        candidates, distance = ids.nearest(raw, known)
        if len(candidates) == 1:
            self._bump_near_miss(key, candidates[0], distance)
            return ids.Resolution(
                id=candidates[0], requested=raw, by_proximity=True, distance=distance or 0
            )
        if len(candidates) > 1:
            # candidate NULL with a distance: ambiguous, not fabricated.
            self._bump_near_miss(key, None, distance)
            raise AmbiguousId(raw, candidates, distance or 0)
        # candidate NULL and distance NULL: nothing within radius. §6.1's alarming case.
        self._bump_near_miss(key, None, None)
        raise NoteNotFound(
            f"no note matches {raw!r}, and nothing is within {ids.RECOVERY_RADIUS} "
            f"word-edit of it. Use context() to find it by what it was about."
        )

    def _bump_near_miss(self, key: str, candidate: str | None, distance: int | None) -> None:
        if self.read_only:
            # A reader observing a miss must not become a writer of one.
            return
        with self.db:
            self.db.execute(
                """INSERT INTO near_miss(requested, candidate, distance, first_seen, hit_count)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(requested) DO UPDATE SET
                     hit_count = hit_count + 1,
                     candidate = excluded.candidate,
                     distance  = excluded.distance""",
                (key, candidate, distance, now()),
            )

    # -------------------------------------------------------------- writes

    @synchronized
    def create_note(
        self, desc: str, text: str, supersedes: Iterable[dict[str, Any]] = ()
    ) -> tuple[str, list[ids.Resolution]]:
        """Create a note and its supersession edges atomically (§3.1)."""
        desc = desc.strip()
        text = text.strip()
        if not desc:
            raise SeshatError("desc is required: it is the only thing visible in triage (§4)")
        if not text:
            raise SeshatError("text is required")

        edges = []
        for edge in supersedes:
            resolution = self.resolve(str(edge["id"]))
            retained = _check_retained(edge["retained"])
            edges.append((resolution, retained, edge.get("why")))

        note_id = self.mint_id()
        stamp = now()
        captured = links.extract(text, exclude=note_id)
        with self.db:
            self.db.execute(
                "INSERT INTO note(id, desc, text, created_at) VALUES (?, ?, ?, ?)",
                (note_id, desc, text, stamp),
            )
            self._index_links(note_id, text)
            for resolution, retained, why in edges:
                # A brand-new note has no descendants, so creation-time
                # supersession cannot cycle (§3.4) -- no check needed here.
                self.db.execute(
                    """INSERT INTO assessment(old_id, new_id, retained, rationale, asserted_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (resolution.id, note_id, retained, why, stamp),
                )
        # Committed first, queued after: the note is safe before any capture is
        # attempted, and a snapshot failure can never fail a write (§6.3).
        self._queue_captures(note_id, captured, desc, text)
        if self._on_note_written:
            self._on_note_written()
        return note_id, [r for r, _, _ in edges]

    def _queue_captures(
        self, note_id: str, found: Iterable[Any], desc: str = "", text: str = ""
    ) -> None:
        """Enqueue URL captures with their expectations. Swallows everything (§1).

        Capture-at-creation is the whole preservation model (§6.6), but a
        stalled or broken snapshot store must degrade to "no witness", never to
        "the note was not written".

        The expectation (§6.9) is derived here, at write time, and copied into
        the snapshot rather than read back from `link` -- `link` is derived and
        re-extractable, so a later revision could otherwise leave a 2024 capture
        being judged against 2026 intent.
        """
        if self.snapshots is None:
            return
        try:
            targets = []
            for link in found:
                if link.kind != "url":
                    continue
                expectation, source = extract_mod.choose_expectation(
                    link.label, desc, links.sentence_containing(text, link.target)
                )
                targets.append((link.target, expectation, source))
            if targets and self.snapshots.enqueue(note_id, targets) and self._on_captures_queued:
                self._on_captures_queued()
        except Exception:
            log.exception("could not queue snapshot capture for %s", note_id)

    @synchronized
    def add_assessment(
        self, old_raw: str, new_raw: str, retained: float, why: str | None = None
    ) -> dict[str, Any]:
        """Record a supersession, or re-assess an existing one (§3.4, §5.5)."""
        old = self.resolve(old_raw)
        new = self.resolve(new_raw)
        retained = _check_retained(retained)
        if old.id == new.id:
            raise CycleError(f"{old.id} cannot supersede itself")
        if self._reaches(new.id, old.id):
            raise CycleError(
                f"{old.id} is already a descendant of {new.id}; the edge would "
                f"make head resolution non-terminating"
            )

        prior = self.db.execute(
            "SELECT retained FROM supersession WHERE old_id = ? AND new_id = ?", (old.id, new.id)
        ).fetchone()

        stamp = now()
        while True:
            try:
                with self.db:
                    self.db.execute(
                        """INSERT INTO assessment(old_id, new_id, retained, rationale, asserted_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (old.id, new.id, retained, why, stamp),
                    )
                break
            except sqlite3.IntegrityError:
                # Same edge, same microsecond. Nudge forward rather than lose one.
                stamp = _tick(stamp)

        return {
            "old_id": old.id,
            "new_id": new.id,
            "retained": retained,
            "reassessment": prior is not None,
            "previous_retained": prior["retained"] if prior is not None else None,
            "asserted_at": stamp,
            "resolved_by_proximity": _proximity(old, new),
        }

    # --------------------------------------------------------------- links

    def _index_links(self, note_id: str, text: str) -> None:
        """Populate the derived `link` index for one note (§6.5).

        Extraction failures are not write failures: a note whose text confuses
        the extractor is stored with fewer links, never rejected (§6.4).
        """
        for link in links.extract(text, exclude=note_id):
            self.db.execute(
                "INSERT OR IGNORE INTO link(note_id, kind, target, label) VALUES (?, ?, ?, ?)",
                (note_id, link.kind, link.target, link.label),
            )

    def _rebuild_links(self) -> None:
        """Drop and re-extract the derived link index.

        SAFE BY CONSTRUCTION, AND ONLY BY CONSTRUCTION. `link` is regenerable
        from note text in seconds. `snapshot` shares its (note_id, target) key
        and is gone forever if dropped -- it is the only unrecoverable data in
        the system. The two live in SEPARATE DATABASE FILES (see snapshots.py)
        precisely so that this method cannot reach the snapshot table even if
        someone edits the statement below. Do not "simplify" that apart.
        """
        self.db.execute("DELETE FROM link")
        for row in self.db.execute("SELECT id, text FROM note").fetchall():
            self._index_links(row["id"], row["text"])

    @synchronized
    def reindex_links(self) -> int:
        with self.db:
            self._rebuild_links()
        return self.db.execute("SELECT COUNT(*) FROM link").fetchone()[0]

    def links_of(self, note_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT kind, target, label FROM link WHERE note_id = ? ORDER BY kind, target",
            (note_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def backlinks_of(self, note_id: str, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Notes whose text references this one (§3.3).

        Not optional, and truncation must carry its count: a hub note is
        exactly the case that overflows, and a truncated list without the total
        reports a hub as a leaf.
        """
        total = self.db.execute(
            "SELECT COUNT(*) FROM link WHERE kind = 'note' AND target = ?", (note_id,)
        ).fetchone()[0]
        rows = self.db.execute(
            """SELECT n.id, n.desc FROM link l JOIN note n ON n.id = l.note_id
               WHERE l.kind = 'note' AND l.target = ?
               ORDER BY n.created_at DESC, n.id LIMIT ?""",
            (note_id, limit),
        ).fetchall()
        return [dict(r) for r in rows], total

    # --------------------------------------------------------------- graph

    def _reaches(self, start: str, target: str) -> bool:
        """Is `target` a descendant of `start` along supersession edges?"""
        row = self.db.execute(
            """WITH RECURSIVE d(id) AS (
                 SELECT ?
                 UNION
                 SELECT s.new_id FROM supersession s JOIN d ON s.old_id = d.id
               )
               SELECT 1 FROM d WHERE id = ? LIMIT 1""",
            (start, target),
        ).fetchone()
        return row is not None

    @synchronized
    def heads(self, note_id: str) -> list[str]:
        """Current heads reachable from a note. A set, never assumed unique (§2).

        A note with no outgoing edges is its own head.
        """
        rows = self.db.execute(
            """WITH RECURSIVE d(id) AS (
                 SELECT ?
                 UNION
                 SELECT s.new_id FROM supersession s JOIN d ON s.old_id = d.id
               )
               SELECT id FROM d WHERE id NOT IN (SELECT old_id FROM supersession)
               ORDER BY id""",
            (note_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    @synchronized
    def chain(self, raw: str) -> dict[str, Any]:
        """Ancestor and descendant closure (§3.5). Raw edge scores, no path aggregation."""
        resolution = self.resolve(raw)
        rows = self.db.execute(
            """WITH RECURSIVE
                 down(id) AS (
                   SELECT ?
                   UNION
                   SELECT s.new_id FROM supersession s JOIN down ON s.old_id = down.id
                 ),
                 up(id) AS (
                   SELECT ?
                   UNION
                   SELECT s.old_id FROM supersession s JOIN up ON s.new_id = up.id
                 ),
                 closure(id) AS (SELECT id FROM down UNION SELECT id FROM up)
               SELECT n.id, n.desc, n.created_at FROM note n JOIN closure c ON c.id = n.id
               ORDER BY n.created_at""",
            (resolution.id, resolution.id),
        ).fetchall()
        node_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(node_ids))
        edges = self.db.execute(
            f"""SELECT old_id, new_id, retained, rationale FROM supersession
                WHERE old_id IN ({placeholders}) AND new_id IN ({placeholders})""",
            node_ids + node_ids,
        ).fetchall()
        return {
            "nodes": [{"id": r["id"], "desc": r["desc"]} for r in rows],
            "edges": [dict(e) for e in edges],
            "resolved_by_proximity": _proximity(resolution),
        }

    # ---------------------------------------------------------------- read

    @synchronized
    def read(
        self, raw: str, edge_limit: int = DEFAULT_EDGE_LIMIT, with_sources: bool = False
    ) -> Record:
        resolution = self.resolve(raw)
        row = self.db.execute(
            "SELECT id, desc, text, created_at FROM note WHERE id = ?", (resolution.id,)
        ).fetchone()

        out_edges, out_total = self._edges("old_id", resolution.id, "new_id", edge_limit)
        in_edges, in_total = self._edges("new_id", resolution.id, "old_id", edge_limit)
        back, back_total = self.backlinks_of(resolution.id, edge_limit)

        return Record(
            id=row["id"],
            desc=row["desc"],
            text=row["text"],
            created_at=row["created_at"],
            supersedes=in_edges,
            supersedes_total=in_total,
            superseded_by=out_edges,
            superseded_by_total=out_total,
            heads=self.heads(resolution.id),
            links=self.links_of(resolution.id),
            backlinks=back,
            backlinks_total=back_total,
            sources=self._sources(resolution.id) if with_sources else None,
            resolved_by_proximity=_proximity(resolution),
        )

    def _sources(self, note_id: str) -> list[dict[str, Any]]:
        """Preserved link content, opt-in because it would otherwise wreck every
        return (§6.6). Verification is adjacency, not a pass."""
        if self.snapshots is None:
            return []
        return self.snapshots.sources_for(note_id)

    def _edges(
        self, anchor: str, note_id: str, other: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Edges on one side of a note, truncated -- with the pre-truncation count.

        The count is not optional: a reader shown five of twelve supersessions
        concludes the note absorbed five things, which is the store lying (§3.3).
        """
        total = self.db.execute(
            f"SELECT COUNT(*) FROM supersession WHERE {anchor} = ?", (note_id,)
        ).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT {other} AS id, retained, rationale FROM supersession
                WHERE {anchor} = ? ORDER BY retained DESC, id LIMIT ?""",
            (note_id, limit),
        ).fetchall()
        return [dict(r) for r in rows], total

    def _fts_ranking(self, query: str, since: str | None, limit: int) -> list[str]:
        params: list[Any] = [self.theta]
        where = ["p.retained >= ?"]
        if since:
            where.append("n.created_at >= ?")
            params.append(since)
        where.append("note_fts MATCH ?")
        params.append(query)
        params.append(limit)
        rows = self.db.execute(
            f"""SELECT n.id FROM note_fts
                JOIN note n ON n.rowid = note_fts.rowid
                JOIN pool_retained p ON p.id = n.id
                WHERE {' AND '.join(where)}
                ORDER BY bm25(note_fts), n.created_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [r["id"] for r in rows]

    def _recent(self, since: str | None, limit: int) -> list[str]:
        params: list[Any] = [self.theta]
        clause = ""
        if since:
            clause = "AND n.created_at >= ?"
            params.append(since)
        params.append(limit)
        rows = self.db.execute(
            f"""SELECT n.id FROM note n JOIN pool_retained p ON p.id = n.id
                WHERE p.retained >= ? {clause}
                ORDER BY n.created_at DESC, n.id LIMIT ?""",
            params,
        ).fetchall()
        return [r["id"] for r in rows]

    def _vector_ranking(
        self, text: str, since: str | None, limit: int
    ) -> tuple[list[str], dict[str, float], list[float]] | None:
        """Semantic ranking, or None if the vector side cannot answer right now.

        None is a legitimate, expected outcome (§6.3): no extension, no
        embedder, a cold Ollama, or simply nothing embedded yet. Every one of
        those degrades `context` to FTS rather than failing it.
        """
        if not self.vector_ready or self.embedder is None:
            return None
        try:
            vector = embeddings.embed_query(self.embedder, text)
        except Exception as exc:
            log.info("vector side unavailable for this query (%s); using FTS only", exc)
            self._vector_cooldown_until = time.monotonic() + VECTOR_COOLDOWN
            return None
        scored = vectors.search(self.db, vector, self.theta, limit, since)
        return [i for i, _ in scored], dict(scored), vector

    @synchronized
    def context(
        self, text: str = "", since: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[Hit]:
        """Primary retrieval (§3.2). Empty text degrades to a recency listing.

        Hybrid when it can be: FTS5/BM25 and vector KNN are run independently
        and fused with RRF (§6). When the vector side cannot answer, the result
        is the FTS ranking alone -- **worse, but never wrong, and never an
        error** (§6.3). `help` reports which of the two is live so a caller can
        say so rather than presenting degraded results as the best available.

        Pool membership is `retained >= theta` (§5.1), computed from direct
        edges only -- no path composition, so the diamond resolves without
        traversal.
        """
        query = fts_query(text)
        if query is None:
            # Empty or unusable query: recency listing. There is nothing for
            # either retriever to match on, so no fusion happens.
            return self._recency_listing(since, limit)

        keyword = self._fts_ranking(query, since, limit)
        contributors: dict[str, list[str]] = {"fts": keyword}
        rankings = [keyword]
        query_vector = None
        semantic = self._vector_ranking(text, since, limit)
        if semantic is not None:
            ids_, _, query_vector = semantic
            contributors["vector"] = ids_
            rankings.append(ids_)
        return self._hydrate(fuse(rankings), limit, contributors, query_vector)

    def _recency_listing(self, since: str | None, limit: int) -> list[Hit]:
        """§3.2: ordered by recency, with every retrieval field null."""
        ordered = self._recent(since, limit)
        if not ordered:
            return []
        descs = dict(
            self.db.execute(
                f"SELECT id, desc FROM note WHERE id IN ({','.join('?' * len(ordered))})",
                ordered,
            ).fetchall()
        )
        return [Hit(note_id, descs[note_id], None, None, None) for note_id in ordered]

    def _hydrate(
        self,
        scores: dict[str, float],
        limit: int,
        contributors: dict[str, list[str]],
        query_vector,
    ) -> list[Hit]:
        """Attach descriptions, similarities and provenance to scored ids (§3.2)."""
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        note_ids = [note_id for note_id, _ in ranked]
        descs = dict(
            self.db.execute(
                f"SELECT id, desc FROM note WHERE id IN ({','.join('?' * len(note_ids))})",
                note_ids,
            ).fetchall()
        )
        # Reported for every returned note, not only those the vector side
        # ranked highly: a keyword-only hit with a low cosine is exactly the
        # case a caller needs to see (§3.2).
        similarity: dict[str, float] = {}
        if query_vector is not None and self.vector_ready:
            try:
                similarity = vectors.similarity_for(self.db, query_vector, note_ids)
            except Exception:  # pragma: no cover - vector side already degraded
                similarity = {}
        membership = {
            note_id: tuple(
                name for name, ranking in contributors.items() if note_id in ranking
            )
            for note_id in note_ids
        }
        return [
            Hit(note_id, descs[note_id], score, similarity.get(note_id), membership[note_id])
            for note_id, score in ranked
        ]

    # ----------------------------------------------------------- embedding

    def needs_embedding(self, limit: int = 64) -> list[tuple[str, str, str]]:
        """Notes with no current embedding (§6.3).

        The queue is a query, not a table: a row's absence from
        `embedding_meta`, or a `model` that is not the current one, means
        "needs embedding". That is what makes model migration a DELETE plus a
        drain rather than a schema change.
        """
        model = self.embedder.model if self.embedder else ""
        rows = self.db.execute(
            """SELECT n.id, n.desc, n.text FROM note n
               LEFT JOIN embedding_meta e ON e.note_id = n.id
               WHERE e.note_id IS NULL OR e.model != ?
               ORDER BY n.created_at LIMIT ?""",
            (model, limit),
        ).fetchall()
        return [(r["id"], r["desc"], r["text"]) for r in rows]

    def embedding_backlog(self) -> int:
        model = self.embedder.model if self.embedder else ""
        return self.db.execute(
            """SELECT COUNT(*) FROM note n LEFT JOIN embedding_meta e ON e.note_id = n.id
               WHERE e.note_id IS NULL OR e.model != ?""",
            (model,),
        ).fetchone()[0]

    @synchronized
    def store_embedding(self, note_id: str, vector: Sequence[float]) -> None:
        from .embeddings import l2_norm

        if not self.vector_ready:
            return
        normalized = abs(l2_norm(vector) - 1.0) < 1e-3
        with self.db:
            vectors.upsert(self.db, note_id, vector)
            self.db.execute(
                """INSERT INTO embedding_meta(note_id, model, dim, normalized, embedded_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(note_id) DO UPDATE SET
                     model = excluded.model, dim = excluded.dim,
                     normalized = excluded.normalized, embedded_at = excluded.embedded_at""",
                (note_id, self.embedder.model, len(vector), int(normalized), now()),
            )

    @synchronized
    def clear_embeddings(self) -> int:
        """Model migration (§6.3): drop provenance, let the worker drain."""
        count = self.db.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0]
        with self.db:
            self.db.execute("DELETE FROM embedding_meta")
            if self.vector_ready:
                self.db.execute("DELETE FROM note_vec")
        return count

    @synchronized
    def search_rationales(self, text: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """Find assessment rationales by text (§5.5). Not part of the MCP surface."""
        query = fts_query(text)
        if query is None:
            return []
        rows = self.db.execute(
            """SELECT a.old_id, a.new_id, a.retained, a.rationale, a.asserted_at
               FROM assessment_fts f JOIN assessment a ON a.rowid = f.rowid
               WHERE assessment_fts MATCH ? ORDER BY bm25(assessment_fts) LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _check_retained(value: Any) -> float:
    retained = float(value)
    if not 0.0 <= retained <= 1.0:
        raise SeshatError(f"retained must be in [0, 1], got {retained}")
    return retained


def _tick(stamp: str) -> str:
    """Advance a timestamp by one microsecond, preserving the sortable format."""
    return (datetime.fromisoformat(stamp) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    )


def _proximity(*resolutions: ids.Resolution) -> dict[str, Any] | None:
    """Report corrected ids. Present only when something was actually corrected (§3.3)."""
    corrected = [r for r in resolutions if r.by_proximity]
    if not corrected:
        return None
    return {
        "corrected": [
            {"requested": r.requested, "resolved_to": r.id, "word_edits": r.distance}
            for r in corrected
        ]
    }
