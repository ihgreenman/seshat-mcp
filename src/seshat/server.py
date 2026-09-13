"""MCP server: the five tools of spec §3.

Increment one is FTS-only, so `context` is keyword retrieval. The tool surface
is exactly the spec's; the vector side changes scores, not signatures.
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

from .store import DEFAULT_EDGE_LIMIT, DEFAULT_LIMIT, DEFAULT_THETA, SeshatError, Store

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
`why` is optional free text explaining the score."""

CONTEXT_DESCRIPTION = """\
Search the note store. Returns (id, desc, score) -- descriptions only; bodies
are paid for individually via `read`.

`text` may be empty, in which case this is a recency listing. That is the
session-start orientation call.

The score is a fused retrieval score, not a relevance guarantee. Low-scoring
results are in the list because they matched something, not because they are
relevant; triage on the score."""

READ_DESCRIPTION = """\
Read a note in full, with its supersession edges.

Always check `superseded_by` and `heads` before acting on the body: an id you
are holding from earlier in the conversation may since have been superseded or
retracted. `heads` gives the current version(s) directly.

`supersedes_total` / `superseded_by_total` are counts BEFORE truncation to
`edge_limit`. If a total exceeds the list length, you are not seeing all of it."""

SUPERSEDES_DESCRIPTION = """\
Record, after the fact, that one existing note supersedes another.

Use this when you realise a note you wrote earlier invalidates or extends an
older one. Called on a pair that already has an edge, it records a
RE-ASSESSMENT of `retained` rather than failing -- the old score is kept in
history. Edges that would make the graph cyclic are rejected."""

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
            Field(description="[{id, retained, why?}] -- notes this one replaces or extends."),
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
        return {"results": [asdict(h) for h in hits], "count": len(hits)}

    @server.tool(name="read", description=READ_DESCRIPTION)
    @reported
    def read_tool(
        id: Annotated[str, Field(description="Note id: four hyphenated words.")],
        edge_limit: Annotated[
            int, Field(description="Max edges listed per direction.", ge=1)
        ] = DEFAULT_EDGE_LIMIT,
    ) -> dict[str, Any]:
        record = asdict(store.read(id, edge_limit))
        if record["resolved_by_proximity"] is None:
            del record["resolved_by_proximity"]
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

    return server


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv)


def serve(db_path: Path | None = None, theta: float = DEFAULT_THETA) -> None:
    path = db_path or default_db_path()
    store = Store(path, theta=theta)
    log.info("seshat store: %s (theta=%s, fts-only)", path, theta)
    build_server(store).run(transport="stdio")
