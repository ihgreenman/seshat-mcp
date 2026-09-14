"""Schema versioning and migration. Spec §11.1, §11.2.

`store_version` is independent of the spec and software versions, and moves only
when a migration is required.
"""

import sqlite3

import pytest

from seshat.store import SCHEMA, SCHEMA_VERSION, SeshatError, Store


def schema_of(path) -> set[str]:
    """Every object SQLite knows about, normalised for whitespace."""
    db = sqlite3.connect(str(path))
    rows = db.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    db.close()
    return {(t, n, " ".join(sql.split())) for t, n, sql in rows}


def make_v1_store(path) -> None:
    """A store as increment one left it: schema v1, no meta, no link table."""
    db = sqlite3.connect(str(path))
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()


def test_fresh_store_is_at_the_current_version(tmp_path):
    s = Store(tmp_path / "fresh.db")
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.meta()["store_version"] == str(SCHEMA_VERSION)
    assert "created_at" in s.meta() and "created_under_spec" in s.meta()
    s.close()


def test_migrated_store_matches_a_fresh_one(tmp_path):
    """The check that keeps migrations honest: a store that grew into version N
    and one created at version N must be structurally identical. Otherwise the
    two diverge quietly and only the fresh path is ever tested."""
    old = tmp_path / "old.db"
    make_v1_store(old)
    Store(old).close()

    fresh = tmp_path / "fresh.db"
    Store(fresh).close()

    assert schema_of(old) == schema_of(fresh)


def test_migration_preserves_existing_notes_and_builds_the_link_index(tmp_path):
    """`link` is derived, so an old store gets it built by re-reading note text
    -- the same path a rebuild takes."""
    path = tmp_path / "old.db"
    make_v1_store(path)
    db = sqlite3.connect(str(path))
    db.execute(
        "INSERT INTO note(id, desc, text, created_at) VALUES (?,?,?,?)",
        ("alpha-bacon-cargo-dune", "an old note",
         "written before links existed, see https://example.com/old",
         "2026-01-01T00:00:00.000000+00:00"),
    )
    db.commit()
    db.close()

    s = Store(path)
    record = s.read("alpha-bacon-cargo-dune")
    assert record.desc == "an old note"
    assert record.links == [{"kind": "url", "target": "https://example.com/old", "label": None}]
    assert s.meta()["store_version"] == str(SCHEMA_VERSION)
    s.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    make_v1_store(path)
    Store(path).close()
    before = schema_of(path)
    Store(path).close()
    assert schema_of(path) == before


def test_a_future_store_is_refused_not_downgraded(tmp_path):
    """Opening a newer store read-write would corrupt it silently."""
    path = tmp_path / "future.db"
    Store(path).close()
    db = sqlite3.connect(str(path))
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()

    with pytest.raises(SeshatError, match="newer than this build"):
        Store(path)


def test_every_migration_step_has_a_script():
    from seshat.store import MIGRATIONS

    assert set(MIGRATIONS) == set(range(2, SCHEMA_VERSION + 1)), (
        "a version bump without a migration script leaves old stores unopenable"
    )
