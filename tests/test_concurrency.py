"""The store lock buys operation-level atomicity, which `check_same_thread=False`
does not.

sqlite3 runs in serialized mode here, so individual statements are already safe.
What is not safe without the lock is a multi-statement operation: `create_note`
mints an id, opens a transaction and inserts a note plus its edges, and
`add_assessment` resolves both ids and checks for a cycle before writing. Two of
those interleaved on one connection can nest transactions and let one
operation's failure roll back another's committed work.

These tests assert non-interleaving directly, because the race itself is not
reproducible on demand.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from seshat.store import Store


def _interleaved(events):
    """True if any thread's operation began inside another's.

    `events` is an ordered list of (phase, thread-id) with phase in
    {"enter", "leave"}.
    """
    open_threads = set()
    for phase, tid in events:
        if phase == "enter":
            if open_threads and tid not in open_threads:
                return True
            open_threads.add(tid)
        else:
            open_threads.discard(tid)
    return False


def test_interleaving_detector_detects_interleaving():
    """Sanity-check the checker before trusting what it says about the store."""
    a, b = 1, 2
    assert not _interleaved([("enter", a), ("leave", a), ("enter", b), ("leave", b)])
    assert _interleaved([("enter", a), ("enter", b), ("leave", a), ("leave", b)])


def test_create_note_does_not_interleave(store, monkeypatch):
    events = []
    real_mint = Store.mint_id

    def instrumented(self):
        events.append(("enter", threading.get_ident()))
        time.sleep(0.02)  # wide enough that an unlocked pair would overlap
        return real_mint(self)

    monkeypatch.setattr(Store, "mint_id", instrumented)

    def worker(i):
        note_id, _ = store.create_note(f"note {i}", "body")
        events.append(("leave", threading.get_ident()))
        return note_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        made = list(pool.map(worker, range(8)))

    assert len(set(made)) == 8
    assert not _interleaved(events), events


def test_add_assessment_does_not_interleave(store, monkeypatch):
    """`resolve` then cycle-check then INSERT is only atomic under the lock."""
    notes = [store.create_note(f"n{i}", "body")[0] for i in range(8)]
    target = store.create_note("target", "body")[0]

    events = []
    real_reaches = Store._reaches

    def instrumented(self, start, end):
        events.append(("enter", threading.get_ident()))
        time.sleep(0.02)
        return real_reaches(self, start, end)

    monkeypatch.setattr(Store, "_reaches", instrumented)

    def worker(note_id):
        store.add_assessment(note_id, target, 0.5)
        events.append(("leave", threading.get_ident()))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, notes))

    assert not _interleaved(events), events
    assert store.db.execute(
        "SELECT COUNT(*) FROM supersession WHERE new_id = ?", (target,)
    ).fetchone()[0] == 8
