import pytest

from seshat.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def mk(store):
    """Create a labelled note and return its id."""

    def _mk(label: str, text: str | None = None) -> str:
        note_id, _ = store.create_note(label, text or f"body of {label}")
        return note_id

    return _mk
