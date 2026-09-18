"""The embedding worker. Spec §6.3.

The single most important implementation constraint in the spec, and it follows
directly from priority 1: **writes must not block on embedding.** If `note()`
embedded synchronously, a cold model load or a stopped Ollama daemon would make
the write *fail* -- and readily-written notes are the entire value proposition.

So: write the row, commit, return the id, embed later. FTS5 is a synchronous
trigger with no external dependency, so a note is keyword-findable immediately
and semantically findable seconds later.

A separate worker from snapshot capture, for the reason §6.6 gives: embedding is
local, fast, and fails atomically; fetching is network-bound, slow, and hangs.
One stalled HTTP request must not block the embedding backlog.
"""

from __future__ import annotations

import logging
import threading

from .embeddings import EmbedderUnavailable, document_text, embed_documents

log = logging.getLogger("seshat.worker")

BATCH = 16
"""Notes per embed call. Ollama handles batches happily and the round trip
dominates for short notes."""


class EmbeddingWorker:
    def __init__(self, store, poll: float = 2.0, batch: int = BATCH, backoff: float = 30.0):
        self.store = store
        self.poll = poll
        self.batch = batch
        self.backoff = backoff
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cooldown = 0.0

    def start(self) -> None:
        if self._thread is not None or self.store.embedder is None:
            return
        self._thread = threading.Thread(target=self._loop, name="seshat-embed", daemon=True)
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
            # Cleared before the work rather than after the wait: the standard
            # ordering, where you drop the signal and then look, so anything
            # arriving after you looked re-raises it.
            #
            # No bug was fixed by moving it, and the review note claiming one
            # was wrong. Traced: a notify during `run_once` sets the event, the
            # wait returns at once and the next `run_once` takes the work; a
            # notify between the wait and the clear is dropped unacted, but the
            # very next statement is `run_once`, which drains whatever is
            # pending. Nothing is ever lost either way. The real defect was
            # below -- a full batch slept a whole poll interval.
            self._wake.clear()
            wait = self.poll
            try:
                embedded = self.run_once()
                if embedded:
                    # A full batch means there is probably another behind it.
                    # Sleeping the poll interval between them drained a backlog
                    # at `batch` notes per `poll` seconds -- 16 per 2s -- when
                    # the embedder was ready to go straight on.
                    continue
            except EmbedderUnavailable as exc:
                # Expected, not exceptional: Ollama is not running, or the
                # model is still loading. Back off and keep the backlog.
                log.info("embedder unavailable (%s); retrying in %.0fs", exc, self.backoff)
                wait = self.backoff
            except Exception:
                log.exception("embedding worker iteration failed")
                wait = self.backoff
            self._wake.wait(wait)

    def run_once(self) -> int:
        """Embed one batch. Returns how many notes were embedded.

        Raises EmbedderUnavailable so the caller can distinguish "nothing to do"
        from "could not do it" -- a distinction `help` needs in order to report
        the backlog honestly.
        """
        store = self.store
        if store.embedder is None or not store.vector_ready:
            return 0
        pending = store.needs_embedding(self.batch)
        if not pending:
            return 0

        texts = [document_text(desc, text) for _, desc, text in pending]
        vectors = embed_documents(store.embedder, texts)
        for (note_id, _, _), vector in zip(pending, vectors):
            store.store_embedding(note_id, vector)
        log.debug("embedded %d notes", len(pending))
        return len(pending)

    def drain(self, max_batches: int = 1000) -> int:
        """Embed everything outstanding. Used by the CLI and by tests."""
        total = 0
        for _ in range(max_batches):
            done = self.run_once()
            if done == 0:
                break
            total += done
        return total
