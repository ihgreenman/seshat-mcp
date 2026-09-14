"""SQLite storage and retrieval for seshat. Spec §5, §6, §7.1.

Increment one: FTS5 only. There is no embedding table, no Ollama dependency and
no background worker here. `context` is therefore keyword retrieval, scored in
the shape RRF will use once the vector side lands (§6, `_rrf`).
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from . import ids, links

if TYPE_CHECKING:  # pragma: no cover
    from .snapshots import SnapshotStore

log = logging.getLogger("seshat.store")

DEFAULT_THETA = 0.8
"""Pool-membership threshold (§5.1). Open question §9.2 -- no principled basis."""

RRF_K = 60
"""Reciprocal rank fusion constant (§6)."""

DEFAULT_LIMIT = 20
DEFAULT_EDGE_LIMIT = 10

SCHEMA_VERSION = 2
"""On-disk schema version -- `store_version` in §11.1, independent of both the
spec version and the software version."""

SCHEMA = """
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

MIGRATION_2 = """
-- Store identity. Written at creation, bumped only on migration. §11.2
CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

-- Derived index over note text. Rebuildable, disposable. §6.5
-- Shares a key with `snapshot`, which is neither -- see snapshots.py.
CREATE TABLE link (
  note_id  TEXT NOT NULL REFERENCES note(id),
  kind     TEXT NOT NULL,         -- 'url' | 'hash' | 'note'
  target   TEXT NOT NULL,
  label    TEXT,                  -- markdown anchor text, if any
  PRIMARY KEY (note_id, kind, target)
);
CREATE INDEX link_target ON link(target);   -- backlinks and DISTINCT target
"""

MIGRATIONS: dict[int, str] = {2: MIGRATION_2}
"""Applied in order to reach SCHEMA_VERSION. A fresh store runs SCHEMA and then
every migration, so a migrated store and a fresh one are byte-identical in
structure -- which `tests/test_migration.py` checks rather than assumes."""


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
    id: str
    desc: str
    score: float


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


_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is quoted, so no caller string can reach FTS5's query syntax.
    OR rather than the default AND: retrieval wants recall with bm25 doing the
    ranking, not a conjunctive filter that returns nothing on a five-word query.
    """
    tokens = _FTS_TOKEN.findall(text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


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
    ):
        self.path = str(path)
        self.theta = theta
        self.snapshots = snapshots
        self._lock = threading.RLock()
        self._on_captures_queued: Any = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()

    def close(self) -> None:
        self.db.close()

    def _ensure_schema(self) -> None:
        """Create or migrate. `PRAGMA user_version` is authoritative; `meta`
        mirrors it for §3.6's reporting contract."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        fresh = version == 0
        if version > SCHEMA_VERSION:
            raise SeshatError(
                f"store {self.path} is schema version {version}, newer than this "
                f"build's {SCHEMA_VERSION}. Upgrade seshat rather than downgrading the store."
            )

        with self.db:
            if fresh:
                self.db.executescript(SCHEMA)
                version = 1
            for target in range(version + 1, SCHEMA_VERSION + 1):
                self.db.executescript(MIGRATIONS[target])
                self._on_migrated(target, fresh)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._write_meta(fresh)

    def _on_migrated(self, target: int, fresh: bool) -> None:
        """Data work a migration needs beyond its DDL."""
        if target == 2 and not fresh:
            # `link` is derived, so an existing store gets its index built the
            # same way a rebuild would -- by re-reading note text (§6.5).
            self._rebuild_links()

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
        self._queue_captures(note_id, captured)
        return note_id, [r for r, _, _ in edges]

    def _queue_captures(self, note_id: str, found: Iterable[Any]) -> None:
        """Enqueue URL captures. Swallows everything -- priority 1 (§1).

        Capture-at-creation is the whole preservation model (§6.6), but a
        stalled or broken snapshot store must degrade to "no witness", never to
        "the note was not written".
        """
        if self.snapshots is None:
            return
        try:
            targets = [link.target for link in found if link.kind == "url"]
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

    @synchronized
    def context(
        self, text: str = "", since: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[Hit]:
        """Primary retrieval (§3.2). Empty text degrades to a recency listing.

        Pool membership is `retained >= theta` (§5.1), computed from direct
        edges only -- no path composition, so the diamond resolves without
        traversal.
        """
        query = fts_query(text)
        params: list[Any] = [self.theta]
        where = ["p.retained >= ?"]
        if since:
            where.append("n.created_at >= ?")
            params.append(since)

        if query is None:
            sql = f"""SELECT n.id, n.desc FROM note n JOIN pool_retained p ON p.id = n.id
                      WHERE {' AND '.join(where)}
                      ORDER BY n.created_at DESC, n.id LIMIT ?"""
        else:
            where.append("note_fts MATCH ?")
            params.append(query)
            sql = f"""SELECT n.id, n.desc FROM note_fts
                      JOIN note n ON n.rowid = note_fts.rowid
                      JOIN pool_retained p ON p.id = n.id
                      WHERE {' AND '.join(where)}
                      ORDER BY bm25(note_fts), n.created_at DESC LIMIT ?"""
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        return [Hit(r["id"], r["desc"], _rrf(i)) for i, r in enumerate(rows, start=1)]

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
