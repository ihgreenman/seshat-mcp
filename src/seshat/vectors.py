"""The vector side of retrieval. Spec §6, §6.3, §6.7.

`sqlite-vec` is an optional extension and pre-v1, so nothing here is allowed to
be load-bearing: if it will not load, `vector_available` is False and retrieval
falls back to FTS. **Unembedded is a legitimate state, not an error** (§6.3),
and so is "no vector support at all".

Search is exact brute force -- a join against every pooled row -- not an ANN
index. At this corpus size that is fast enough (§6.7), it keeps the blast radius
of an upstream break small, and it is the only form that filters by pool
membership and `since` exactly rather than post-filtering a truncated candidate
list into nothing.
"""

from __future__ import annotations

import logging
import sqlite3

from .embeddings import DEFAULT_DIM, pack

log = logging.getLogger("seshat.vectors")


def load_extension(db: sqlite3.Connection) -> bool:
    """Try to attach sqlite-vec. Returns whether the vector side is usable."""
    try:
        import sqlite_vec
    except ImportError:
        log.info("sqlite-vec not installed; retrieval is FTS-only")
        return False
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        return True
    except Exception as exc:
        log.warning("sqlite-vec failed to load (%s); retrieval is FTS-only", exc)
        return False
    finally:
        try:
            db.enable_load_extension(False)
        except Exception:  # pragma: no cover - platform dependent
            pass


def ensure_table(db: sqlite3.Connection, dim: int = DEFAULT_DIM) -> None:
    """Create `note_vec` if it is missing.

    Kept out of the migration DDL deliberately: a `vec0` table in the schema
    would make the database unopenable in any process without the extension,
    which would turn an optional dependency into a required one.
    """
    db.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS note_vec USING vec0(
              note_id TEXT PRIMARY KEY,
              embedding float[{dim}]
            )"""
    )


def stored_dim(db: sqlite3.Connection) -> int | None:
    """The dimension `note_vec` was created with, read back from its DDL."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='note_vec'"
    ).fetchone()
    if row is None or not row[0]:
        return None
    import re

    match = re.search(r"float\[(\d+)\]", row[0])
    return int(match.group(1)) if match else None


def upsert(db: sqlite3.Connection, note_id: str, vector) -> None:
    """Replace a note's vector. Notes are immutable, but re-embedding is not:
    a model migration rewrites every row (§6.3)."""
    db.execute("DELETE FROM note_vec WHERE note_id = ?", (note_id,))
    db.execute(
        "INSERT INTO note_vec(note_id, embedding) VALUES (?, ?)", (note_id, pack(vector))
    )


def search(
    db: sqlite3.Connection,
    vector,
    theta: float,
    limit: int,
    since: str | None = None,
) -> list[str]:
    """Pooled note ids by ascending cosine distance.

    The pool filter is applied inside the query rather than afterwards, so a
    retracted note cannot consume one of the `limit` slots (§5.1).
    """
    params: list = [pack(vector), theta]
    clause = ""
    if since:
        clause = "AND n.created_at >= ?"
        params.append(since)
    params.append(limit)
    rows = db.execute(
        f"""SELECT n.id AS id, vec_distance_cosine(v.embedding, ?) AS d
            FROM note_vec v
            JOIN note n ON n.id = v.note_id
            JOIN pool_retained p ON p.id = n.id
            WHERE p.retained >= ? {clause}
            ORDER BY d, n.created_at DESC
            LIMIT ?""",
        params,
    ).fetchall()
    return [row["id"] for row in rows]
