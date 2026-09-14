# seshat-mcp

A persistent note store an LLM assistant can query and write cheaply.
Design and rationale: [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md).

**Built against spec version 1.1.** The version is declared in the spec header
and recorded as `seshat.SPEC_VERSION`; `help` and `seshat info` report it
alongside the software and store versions, which move independently (§11.1).
`tests/test_spec_version.py` fails if the code ever claims a revision the
document has not reached.

**Status.** The full 1.1 tool surface is implemented. Retrieval is FTS5/BM25 —
there is no embedder yet — and snapshot capture stores raw bytes without
extraction. Both gaps are reported by `help`, not hidden: `vector: false`,
`snapshot_extraction: false`. See [Not yet built](#not-yet-built).

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python ≥ 3.11. Runtime dependencies: `mcp`, `pydantic`. Nothing else — the
store is stdlib `sqlite3`, fetching is stdlib `urllib`, and the BIP-39 wordlist
is vendored.

## Run

```sh
seshat --db ~/notes/seshat.db serve          # MCP server on stdio
seshat --db ~/notes/seshat.db --no-snapshots serve
```

As an MCP server (`claude mcp add`, or in a config file):

```json
{
  "mcpServers": {
    "seshat": {
      "command": "/path/to/.venv/bin/seshat",
      "args": ["serve"],
      "env": { "SESHAT_DB": "~/notes/seshat.db" }
    }
  }
}
```

Without `--db` or `$SESHAT_DB` the store lands at
`~/.local/share/seshat/seshat.db`, with snapshots beside it at
`seshat.db.snapshots.db`.

## Tool surface

The six tools of §3 — `note`, `context`, `read`, `supersedes`, `chain`, and
`help`. The surface does not grow beyond that: maintenance lives in the CLI,
because every tool costs context on every turn.

```sh
seshat info                 # versions, capabilities, counts, pool size
seshat context "nyquist"    # search; empty query is a recency listing
seshat read <id>
seshat chain <id>
seshat why "polyphase"      # search assessment rationales (§5.5)
seshat misses               # id lookups that failed (§6.1)
seshat snapshots            # capture histogram (§6.6)
seshat fetch                # drain the capture queue now
seshat reindex-links        # rebuild the derived link index (§6.5)
```

`seshat misses` classifies each failure, which is the point of the table:

| shown as | meaning |
|---|---|
| `corrupted` | a real id one word-edit away; benign, and the row caches the correction |
| `ambiguous` | several real ids equidistant; resolving would have been a guess |
| `FABRICATED` | nothing within distance — the id was invented, not mistyped |

## Snapshots, and the thing to be careful about

Writing a note extracts the URLs in its text and fetches them once, in the
background (§6.6). **This means a write triggers outbound network requests.** It
is on by default because capture is once-only — a link written while capture is
off is unrecoverable later — but `--no-snapshots` or `SESHAT_SNAPSHOTS=0` turns
it off entirely, and the write path never touches the network itself (a test
pins that).

This build **captures but does not extract**: it stores the raw response bytes
and a SHA-256 over the complete body, and leaves `text`, `title` and `full_hash`
null. That ordering is deliberate — extraction is re-runnable over stored bytes,
an unfetched page is not.

> **The one genuinely dangerous line in this design.** `link` and `snapshot`
> share a `(note_id, target)` primary key and have opposite recoverability:
> `DELETE FROM link` is a safe rebuild that completes in seconds, and
> `DELETE FROM snapshot` is unrecoverable data loss. They live in **separate
> database files** so that the rebuild cannot reach the snapshot table even if
> someone edits the statement. Two tests assert the separation structurally —
> the notes database has no `snapshot` table and the snapshot database has no
> `link` table. Do not consolidate them.

## Implementation notes

Things a reader of the spec would want to know about this build. Spec sections
in brackets.

- **Pool membership is a `LEFT JOIN` with `COALESCE(MAX(...), 1.0)`** [§5.1].
  An inner join type-checks, runs, and silently drops every never-superseded
  note — i.e. most of the store — while `context` keeps returning the revised
  ones and looks fine.
- **`retained` combines by maximum across paths** [§5.2], tested by shuffling a
  fixed edge set through six insertion orders and asserting identical pool
  membership — associativity, commutativity and idempotence in one test.
- **No aggregation along paths** [§5.3]. `chain` returns raw per-edge scores.
- **Re-assessment is append-only** [§5.5], latest-wins via a view.
- **Truncation is always visible** [§3.3], including `backlinks_total` — a hub
  note is exactly the case that overflows.
- **Cycles are rejected at write time** [§3.4] by reachability check.
- **Extraction never fails a write** [§6.4]. Malformed markdown yields fewer
  links, never an error; fenced code blocks are skipped so a URL in a sample
  is not mistaken for a reference.
- **Capture never fails a write** [§1, §6.3]. The note commits first, captures
  queue after, and a broken snapshot store degrades to "no witness".
- **The capture queue is durable**, so a crash between writing a note and
  fetching its links does not lose a capture that can never be taken again.
- **Snapshots are immutable** [§6.6]. A re-queued capture cannot overwrite an
  earlier witness; two notes citing one target get two rows, and comparing them
  is the drift test.
- **Errors carry their guidance to the model**: a store error becomes an MCP
  `ToolError`, so `NoteNotFound` arrives as "use `context()` to find it by what
  it was about" rather than a generic crash.
- **Logging is pinned to stderr**, with a test that reads the raw pipe and
  parses every line as JSON-RPC.
- **One connection plus one lock.** Sync tool bodies run in a worker thread
  pool, so the connection is opened `check_same_thread=False`; the lock spans
  whole operations, because `resolve` → cycle-check → `INSERT` is only atomic
  if nothing interleaves.

### Places this build decides something the spec left open

1. **FTS queries are OR, not AND** — recall with BM25 ranking, rather than a
   conjunctive filter that returns nothing on a five-word question. Tokens are
   quoted, so no caller string reaches FTS5's query syntax.
2. **`near_miss` distinguishes ambiguity from fabrication** within the spec's
   columns: `candidate` NULL with a non-NULL `distance` means several real ids
   were equidistant; both NULL means nothing was in range.
3. **`raw` is an extra table in the snapshot database**, holding the bytes the
   spec's `snapshot` row does not have a column for. It is the hedge that makes
   extraction re-runnable, and a future extractor fills the derived columns
   without rewriting the witness.
4. **A migrated store has no `created_under_spec`** — it was created before the
   `meta` table existed, and inventing a value would be worse than a null.

`θ` defaults to 0.8 [§9.2] — a starting point with no principled basis,
tunable with `--theta`.

### One spec claim this build could not verify

§6 says omitting nomic's `search_document:` / `search_query:` prefixes, or using
the same one on both sides, "degrades retrieval measurably and silently". A
probe against the live model (15 notes, 12 queries, four prefix schemes) could
not detect any difference — all four schemes scored within noise of each other.
That is a **null result, not a refutation**: twelve queries cannot resolve an
effect this size. The prefixes are the documented usage and cost nothing, so
they will be implemented as specified when the embedder lands, but the word
"measurably" should be treated as unverified until a real query set exists.

## Tests

```sh
.venv/bin/python -m pytest
```

126 tests, no network access. Expected values are derived independently of the
implementation (`tests/reference.py` re-derives pool membership, latest-wins
resolution, heads and Levenshtein distance from the spec text and imports
nothing from `seshat`), and the load-bearing assertions have been checked by
mutating the implementation to confirm they turn red — including the pool join,
the max/min rule, truncation counts, silent id resolution, the store lock,
stdout discipline, snapshot immutability, and write-path isolation.

## Not yet built

- **Embeddings and hybrid retrieval** [§6, §6.3] — increment 2. `context`
  scores are already in RRF shape (`1/(k+rank)`, k=60), so the vector side
  changes scores, not signatures.
- **Snapshot extraction** [§6.6] — the `thin` status cannot be assessed until
  an extractor exists, and `help` reports `snapshot_extraction: false` rather
  than letting `snapshots: true` imply it.
- **The consistency checker** [§7] — increment 3, a CLI subcommand.
- **The review interface** [§10] — increment 4.
- **Growth management** [§8] — unsolved in the spec, deliberately not guessed
  at here.

## License

The spec is © 2026 Ian Greenhoe, MIT. See `CREDITS.md` for third-party material.
