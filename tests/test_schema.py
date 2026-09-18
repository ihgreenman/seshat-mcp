"""Schema identity and the one-time reset. Spec §11.1, §11.2.

`store_version` is independent of the spec and software versions. Version 4
consolidated 1-3: spec 1.3 needed a column on a table nothing had populated yet,
and a one-time reset was authorised instead of a migration chain.

The behaviour that matters here is what happens to a store this build cannot
open. It must be refused with instructions -- never upgraded on a guess, and
never deleted on the store's own initiative.
"""

import sqlite3

import pytest

from seshat.store import SCHEMA_VERSION, Store, StoreVersionError


def test_fresh_store_is_at_the_current_version(tmp_path):
    s = Store(tmp_path / "fresh.db")
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.meta()["store_version"] == str(SCHEMA_VERSION)
    assert "created_at" in s.meta() and "created_under_spec" in s.meta()
    s.close()


def test_reopening_is_idempotent(tmp_path):
    path = tmp_path / "s.db"
    first = Store(path)
    note_id, _ = first.create_note("persisted", "body")
    first.close()

    again = Store(path)
    assert again.read(note_id).desc == "persisted"
    assert again.meta()["store_version"] == str(SCHEMA_VERSION)
    again.close()


def test_an_older_store_is_refused_with_instructions(tmp_path):
    """No migration path into 4. The message has to tell the user what to do,
    because the alternative -- guessing at an upgrade -- risks the data."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE note (id TEXT PRIMARY KEY, desc TEXT, text TEXT, created_at TEXT)")
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()

    with pytest.raises(StoreVersionError) as excinfo:
        Store(path)
    message = str(excinfo.value)
    assert "seshat reset" in message, "tell the user the way out"
    assert "no migration path" in message.lower()


def test_a_refused_store_is_left_untouched(tmp_path):
    """The only thing worse than an unopenable note store is one that opens
    empty. Refusing must not be a euphemism for clearing."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE note (id TEXT PRIMARY KEY, desc TEXT, text TEXT, created_at TEXT)")
    old.execute("INSERT INTO note VALUES ('a-b-c-d', 'precious', 'body', '2026-01-01')")
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()
    before = path.read_bytes()

    with pytest.raises(StoreVersionError):
        Store(path)
    assert path.read_bytes() == before


def test_a_future_store_is_refused_not_downgraded(tmp_path):
    path = tmp_path / "future.db"
    Store(path).close()
    db = sqlite3.connect(str(path))
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()

    with pytest.raises(StoreVersionError, match="Upgrade seshat"):
        Store(path)


def test_the_schema_is_one_script_with_no_migration_chain():
    """Consolidation is the point: two ways to arrive at a schema is two things
    to keep in step, and the fresh path is the only one anyone tests."""
    import seshat.store as module

    assert not hasattr(module, "MIGRATIONS")
    assert module.SCHEMA.count("CREATE TABLE note ") == 1


def test_an_unopenable_store_reports_rather_than_crashing(tmp_path, capsys):
    """The refusal's recovery instructions are the whole value of the check.
    Delivered as the last line of a traceback they read as a crash -- and under
    `seshat serve` the MCP client sees the server die instead of saying why.
    """
    import sqlite3

    from seshat.cli import main

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()

    for command in ("info", "check", "serve", "review"):
        assert main(["--db", str(path), "--no-embeddings", command]) == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "error:" in captured.err
        # The instructions have to survive, not just the failure.
        assert "seshat reset" in captured.err, f"{command} lost the recovery advice"
