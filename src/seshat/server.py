"""MCP server: the six tools of spec §3 -- five data operations plus `help`.

Retrieval is FTS-only for now, so `context` is keyword search; `help` reports
that honestly via `capabilities.vector`. The tool surface is exactly the
spec's, and the vector side will change scores rather than signatures.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import SPEC_VERSION, __version__
from .embeddings import DEFAULT_MODEL, OllamaEmbedder
from .ids import looks_like_id
from .snapshots import SnapshotStore, SnapshotWorker, snapshot_path_for
from .worker import EmbeddingWorker
from .store import (
    DEFAULT_EDGE_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_THETA,
    SCHEMA_VERSION,
    SeshatError,
    Store,
)

# Nothing but JSON-RPC on stdout. A stray print on the stdio transport surfaces
# as a malformed protocol frame and reads like an SDK bug, so the root logger is
# pinned to stderr before anything else runs.
logging.basicConfig(stream=sys.stderr, level=os.environ.get("SESHAT_LOG", "INFO"))
log = logging.getLogger("seshat")

NOTE_DESCRIPTION = """\
Write a note to the persistent store. Notes survive across sessions.

Notes are immutable. There is no update and no delete: to correct, extend or
re-word an existing note, write a new one and list the old one in `supersedes`.

`desc` is the most important field. It is the only thing retrieval shows you
later, so write it to be RECOGNISED, not to summarise:
  good: "Bilinear transform loses precision near Nyquist"
  bad:  "notes on filter precision"

`supersedes` entries are {id, retained, why?}. `retained` in [0,1] is the
fraction of the OLD note that survives in the new one -- not your confidence in
the new one:
  1.0  the new note adds; the old is still entirely correct
  0.5  the old note was partly right
  0.0  full retraction; the old note was wrong
`why` is optional free text explaining the score. One score conflates two
different facts, so when `retained` records a RESTRICTION rather than a
DEGRADATION, open `why` with one of these prefixes:

  scope:   the old note is correct within stated bounds; the new one narrows it
  wrong:   the old note was mistaken in the part not retained
  source:  superseded by better evidence, not by better reasoning

  scope: holds for fixed-point implementations; the float path is unaffected

That set is closed. Anything else is free prose -- do not invent `narrowed:`
or `partial:`, because an open vocabulary is unparseable within months.

TWO THINGS ABOUT WRITING NOTES WELL:

1. Use markdown links with real anchor text. `[texas population data](url)`
   states what you wanted from that page, and seshat stores that alongside the
   captured content so a paywall or a moved page can be spotted later. "here"
   or "this article" carries no such information. Reference-style links carry
   it too: `[rate limits][rl]` works as well as the inline form. Autolinks and
   bare URLs carry none, so a note citing one is judged against its `desc`.

2. Keep a capture and an analysis as SEPARATE notes. One note says what a
   source contains; a second references the first by id and says what follows
   from it. Merged, a later correction cannot say whether the page was misread
   or the inference was wrong -- split, superseding the analysis leaves the
   capture untouched, and "my reading changed, the data did not" stays
   readable."""

CONTEXT_DESCRIPTION = """\
Search the note store. Returns descriptions only; bodies are paid for
individually via `read`.

`text` may be empty, in which case this is a recency listing. That is the
session-start orientation call.

Each result carries three things, and they mean different things:

  score              Fused rank score. A SORT KEY ONLY -- it is derived from
                     position, so a perfect match and a worthless one-word
                     match both score ~0.016 at rank 1. Comparable within one
                     result set; meaningless across them. Do NOT read it as
                     confidence.
  vector_similarity  Raw cosine, -1..1, or null if the note is unembedded or
                     the vector side is unavailable. THIS is the number that
                     can actually be low, and the one to triage on. If every
                     result has a low similarity, the store probably holds
                     nothing relevant -- say so rather than reporting the best
                     of a bad list.
  matched            Which retrievers found it: ["fts"], ["vector"], both, or
                     ["recency"] for an empty query. A result matched only by
                     "fts" with low similarity is a keyword coincidence.

`context` searches CONTENT; it does not resolve identifiers. Passing a note id
returns notes whose text mentions it, never the note that has it -- use `read`
for that."""

READ_DESCRIPTION = """\
Read a note in full, with its supersession edges and its references.

Always check `superseded_by` and `heads` before acting on the body: an id you
are holding from earlier in the conversation may since have been superseded or
retracted. `heads` gives the current version(s) directly.

`supersedes_total` / `superseded_by_total` / `backlinks_total` are counts BEFORE
truncation to `edge_limit`. If a total exceeds the list length, you are not
seeing all of it -- and a hub note is exactly the case that overflows.

`links` are references found in this note's text; `backlinks` are notes whose
text points at this one, which is the direction you cannot discover by reading.
`with_sources=True` adds the preserved content of those links -- verbose, so ask
for it only when you need to check what a source actually said. Each source
carries `expectation` (what the citation said it wanted) and
`expectation_met` (whether the captured text contains it). A false
`expectation_met` is a REVIEW CANDIDATE, not a verdict: a statistical table can
contain none of the expected words and still be a perfect capture."""

SUPERSEDES_DESCRIPTION = """\
Record, after the fact, that one existing note supersedes another.

Use this when you realise a note you wrote earlier invalidates or extends an
older one. Called on a pair that already has an edge, it records a
RE-ASSESSMENT of `retained` rather than failing -- the old score is kept in
history. Edges that would make the graph cyclic are rejected.

`why` takes the same closed prefix set as `note`, for the same reason -- a bare
score cannot say whether the old note was WRONG or merely OVERGENERALISED:

  scope:   the old note is correct within stated bounds; the new one narrows it
  wrong:   the old note was mistaken in the part not retained
  source:  superseded by better evidence, not by better reasoning

Anything else is free prose. Do not extend the set."""

HELP_DESCRIPTION = """\
Report what this server actually is: which revision of the seshat spec it
implements, its own version, the schema version of the open store, and which
capabilities are live.

Check `capabilities` before trusting retrieval quality. `vector: false` or a
nonzero `embedding_backlog` means `context` is currently keyword-weighted, and
you can say so rather than presenting worse results as if they were the best
available. `snapshot_status` is a histogram rather than a counter because a
permanently failed capture has already left the queue -- a bare backlog would
read zero while the data is missing."""

CHAIN_DESCRIPTION = """\
Return the ancestor and descendant closure of a note: how a belief got here and
what became of it. Nodes are (id, desc); edges carry raw per-edge `retained`.

Scores are NOT aggregated along paths, deliberately: two hops at 0.5 bound the
surviving fraction only to [0, 0.5], and any single number would be false
precision. Judge the path yourself."""


def reported(fn):
    """Let a store error's message reach the model.

    The SDK turns an unrecognised exception into a generic "error executing
    tool" and keeps the text server-side. That text is the whole value here:
    "nothing is within one word-edit, use context() instead" tells the caller
    what to do next, where the generic message tells it only to give up.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SeshatError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def capability_report(store: Store) -> dict[str, Any]:
    """§3.6. Capabilities are not optional: during incremental development a
    server legitimately implements the full tool surface with no embedder, and
    reporting a bare spec_version would overstate it."""
    snapshots = store.snapshots
    return {
        "vector": bool(store.vector_ready and store.embedder is not None),
        # Shipped, but as a CLI subcommand (`seshat check`) rather than a tool:
        # §7 is periodic maintenance, not something needed mid-conversation.
        "checker": True,
        "snapshots": snapshots is not None,
        # §3.6: `thin: 0` is a false zero without extraction, indistinguishable
        # from "no thin captures". The capability and the histogram's
        # `unextracted` count answer different questions and both are needed.
        "extraction": True,
        "embedding_model": store.embedder.model if store.embedder else None,
        "embedding_backlog": store.embedding_backlog(),
        "snapshot_status": (
            snapshots.histogram()
            if snapshots is not None
            else {"pending": 0, "ok": 0, "unreachable": 0, "gone": 0, "thin": 0}
        ),
    }


def help_payload(store: Store) -> dict[str, Any]:
    meta = store.meta()
    return {
        "spec_version": SPEC_VERSION,
        "software_version": __version__,
        "store_version": int(meta.get("store_version", SCHEMA_VERSION)),
        "created_under_spec": meta.get("created_under_spec"),
        "capabilities": capability_report(store),
    }


def default_db_path() -> Path:
    env = os.environ.get("SESHAT_DB")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "seshat" / "seshat.db"


def build_server(store: Store) -> MCPServer:
    server = MCPServer(
        name="seshat",
        instructions=(
            "Persistent note store with continuity across sessions. Start a session "
            "with context(\"\") to orient. Notes are append-only: correct them by "
            "writing a new note that supersedes the old one, never by rewriting."
        ),
    )

    @server.tool(name="note", description=NOTE_DESCRIPTION)
    @reported
    def note_tool(
        desc: Annotated[str, Field(description="Recognition handle. Specific, not a topic label.")],
        text: Annotated[str, Field(description="The body of the note.")],
        supersedes: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description=(
                    "[{id, retained, why?}] -- notes this one replaces or extends. "
                    "The rationale argument is `why`; an unknown key is rejected, "
                    "not ignored."
                ),
                # The store enforces this -- every caller goes through it, not
                # just this tool -- and produces the message that names the
                # mistake. The schema states the same rule so a client that
                # validates rejects it a round-trip earlier; test_server pins
                # the two together. The whole `anyOf` is replaced rather than a
                # sibling `items` added, because pydantic's generated branch
                # says additionalProperties true and leaving both in place
                # would state one rule twice with two different answers.
                json_schema_extra={
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "retained": {
                                        "type": "number", "minimum": 0, "maximum": 1
                                    },
                                    "why": {"type": "string"},
                                },
                                "required": ["id", "retained"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
            ),
        ] = None,
    ) -> dict[str, Any]:
        note_id, resolutions = store.create_note(desc, text, supersedes or [])
        result: dict[str, Any] = {"id": note_id}
        corrected = [r for r in resolutions if r.by_proximity]
        if corrected:
            result["resolved_by_proximity"] = {
                "corrected": [
                    {"requested": r.requested, "resolved_to": r.id, "word_edits": r.distance}
                    for r in corrected
                ]
            }
        return result

    @server.tool(name="context", description=CONTEXT_DESCRIPTION)
    @reported
    def context_tool(
        text: Annotated[str, Field(description="Query. Empty means recency listing.")] = "",
        since: Annotated[
            str | None, Field(description="ISO 8601 lower bound on creation time.")
        ] = None,
        limit: Annotated[int, Field(description="Maximum results.", ge=1)] = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        hits = store.context(text, since, limit)
        payload: dict[str, Any] = {
            "results": [asdict(h) for h in hits],
            "count": len(hits),
        }
        if looks_like_id(text):
            # §3.2: the division between symbol- and content-addressing is
            # correct but silent, and this is exactly when it bites.
            payload["note"] = (
                f"{text!r} looks like a note id. `context` searches content and "
                f"cannot return the note that has this id -- these results, if any, "
                f"are notes whose text mentions it. Use `read` to fetch it, which "
                f"also recovers from a single mistyped word."
            )
        return payload

    @server.tool(name="read", description=READ_DESCRIPTION)
    @reported
    def read_tool(
        id: Annotated[str, Field(description="Note id: four hyphenated words.")],
        edge_limit: Annotated[
            int, Field(description="Max edges and backlinks listed.", ge=1)
        ] = DEFAULT_EDGE_LIMIT,
        with_sources: Annotated[
            bool,
            Field(description="Include preserved content of this note's links. Verbose."),
        ] = False,
    ) -> dict[str, Any]:
        record = asdict(store.read(id, edge_limit, with_sources))
        if record["resolved_by_proximity"] is None:
            del record["resolved_by_proximity"]
        if record["sources"] is None:
            del record["sources"]
        return record

    @server.tool(name="supersedes", description=SUPERSEDES_DESCRIPTION)
    @reported
    def supersedes_tool(
        old_id: Annotated[str, Field(description="The note being superseded.")],
        new_id: Annotated[str, Field(description="The note that supersedes it.")],
        retained: Annotated[
            float, Field(description="Fraction of the OLD note that survives.", ge=0.0, le=1.0)
        ],
        why: Annotated[str | None, Field(description="Why this score.")] = None,
    ) -> dict[str, Any]:
        result = store.add_assessment(old_id, new_id, retained, why)
        if result["resolved_by_proximity"] is None:
            del result["resolved_by_proximity"]
        return result

    @server.tool(name="chain", description=CHAIN_DESCRIPTION)
    @reported
    def chain_tool(
        id: Annotated[str, Field(description="Note id: four hyphenated words.")],
    ) -> dict[str, Any]:
        result = store.chain(id)
        if result["resolved_by_proximity"] is None:
            del result["resolved_by_proximity"]
        return result

    @server.tool(name="help", description=HELP_DESCRIPTION)
    @reported
    def help_tool() -> dict[str, Any]:
        return help_payload(store)

    return server


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv)


def _open_store(path, **kwargs) -> Store:
    """Open the notes store, or say plainly why it cannot be opened.

    Logged to stderr as well as raised: under stdio transport the client shows
    the process's stderr, and an operator reading a client log needs the
    recovery instructions there rather than in a traceback.
    """
    from .store import StoreVersionError

    try:
        return Store(path, **kwargs)
    except StoreVersionError as exc:
        log.error("cannot open %s: %s", path, exc)
        raise


def serve(
    db_path: Path | None = None,
    theta: float = DEFAULT_THETA,
    snapshots: bool = True,
    embeddings: bool = True,
    model: str = DEFAULT_MODEL,
) -> None:
    path = db_path or default_db_path()
    snapshot_store = SnapshotStore(snapshot_path_for(path)) if snapshots else None
    embedder = OllamaEmbedder(model=model) if embeddings else None
    store = _open_store(path, theta=theta, snapshots=snapshot_store, embedder=embedder)

    embed_worker = None
    if embedder is not None and store.vector_loaded:
        # Off the write path entirely (§6.3): note() commits and returns, the
        # worker catches up. Unembedded is a legitimate state, not an error.
        embed_worker = EmbeddingWorker(store)
        store._on_note_written = embed_worker.notify
        embed_worker.start()
        log.info("embedder: %s (%d notes to embed)", model, store.embedding_backlog())
    elif embedder is not None:
        log.info("sqlite-vec unavailable; retrieval is FTS-only")
    else:
        log.info("embeddings disabled; retrieval is FTS-only")

    worker = None
    if snapshot_store is not None:
        # Capture runs off the write path entirely (§6.3). The store only ever
        # nudges the worker; it never waits for it.
        worker = SnapshotWorker(snapshot_store)
        store._on_captures_queued = worker.notify
        worker.start()
        log.info("snapshot capture: %s (%d queued)",
                 snapshot_store.path, len(snapshot_store.pending()))
    else:
        log.info("snapshot capture: disabled -- links written now are unrecoverable later")

    retrieval = "hybrid" if (embed_worker is not None and store.vector_ready) else "fts-only"
    log.info("seshat store: %s (theta=%s, retrieval=%s)", path, theta, retrieval)
    try:
        build_server(store).run(transport="stdio")
    finally:
        if worker is not None:
            worker.stop()
        if embed_worker is not None:
            embed_worker.stop()
