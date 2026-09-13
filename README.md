# seshat-mcp

A persistent note store an LLM assistant can query and write cheaply.
Design and rationale: [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md).

**Built against spec version 1.0.** The version is declared in the spec header
and recorded in the code as `seshat.SPEC_VERSION`; `seshat info` reports it, and
`tests/test_spec_version.py` fails if the code ever claims a revision the
document has not reached. A spec revision landing ahead of the implementation is
normal and shows up as a skipped test naming the gap.

**Status: increment one — FTS-only.** Everything in §§2–5 of the spec is
implemented and tested. Retrieval is FTS5/BM25; there is no embedding table,
no Ollama dependency, and no background worker yet. See
[Not yet built](#not-yet-built).

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python ≥ 3.11. Runtime dependencies: `mcp`, `pydantic`. Nothing else — the
store is stdlib `sqlite3`, and the BIP-39 wordlist is vendored.

## Run

```sh
seshat --db ~/notes/seshat.db serve     # MCP server on stdio
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
`~/.local/share/seshat/seshat.db`.

## Tool surface

Exactly the five tools of §3 — `note`, `context`, `read`, `supersedes`,
`chain`. Every tool costs context on every turn, so the surface does not grow:
maintenance lives in the CLI.

```sh
seshat info                 # path, counts, pool size, theta
seshat context "nyquist"    # search; empty query is a recency listing
seshat read <id>
seshat chain <id>
seshat why "polyphase"      # search assessment rationales (§5.5)
seshat misses               # id lookups that failed (§6.1)
```

`seshat misses` classifies each failure, which is the point of the table:

| shown as | meaning |
|---|---|
| `corrupted` | a real id one word-edit away; benign, and the row caches the correction |
| `ambiguous` | several real ids equidistant; resolving would have been a guess |
| `FABRICATED` | nothing within distance — the id was invented, not mistyped |

## Implementation notes

Things a reader of the spec would want to know about this build. Spec sections
in brackets.

- **Pool membership is a `LEFT JOIN` with `COALESCE(MAX(...), 1.0)`** [§5.1].
  An inner join type-checks, runs, and silently drops every never-superseded
  note — i.e. most of the store — while `context` keeps returning the revised
  ones and looks fine. `tests/test_pool.py` pins this against a plain-Python
  reading of §5.1 and against a mutation of the join.
- **`retained` combines by maximum across paths** [§5.2]. The order-independence
  property is tested by shuffling a fixed edge set through six insertion orders
  and asserting identical pool membership — associativity, commutativity and
  idempotence in one test.
- **No aggregation along paths** [§5.3]. `chain` returns raw per-edge scores.
- **Re-assessment is append-only** [§5.5]. `supersedes` on an existing pair
  inserts a new assessment; the view resolves latest-wins. `previous_retained`
  comes back in the result so a caller can see what moved.
- **Truncation is always visible** [§3.3]. `_total` counts are computed before
  the limit, never from the truncated list.
- **Cycles are rejected at write time** [§3.4], by reachability check, not by
  the checker after the fact.
- **Errors carry their guidance to the model.** A store error becomes an MCP
  `ToolError` rather than a generic crash, so `NoteNotFound` arrives as "use
  `context()` to find it by what it was about" instead of "error executing
  tool".
- **Logging is pinned to stderr before anything else runs**, and a test asserts
  on the raw pipe that stdout carries only JSON-RPC frames.
- **One connection plus one lock.** The SDK runs sync tool bodies in a worker
  thread pool, so the connection is opened with `check_same_thread=False`; the
  lock spans whole operations, because `resolve` → cycle-check → `INSERT` is
  only atomic if nothing interleaves. `tests/test_concurrency.py` asserts
  non-interleaving directly, since the race is not reproducible on demand.

### Two places this build decides something the spec left open

1. **FTS queries are OR, not AND.** Free text is tokenised and each token
   quoted, so no caller string reaches FTS5's query syntax. `OR` because
   retrieval wants recall with BM25 ranking, not a conjunctive filter that
   returns nothing on a five-word question.
2. **`near_miss` distinguishes ambiguity from fabrication** within the spec's
   columns: `candidate IS NULL AND distance IS NOT NULL` means several real ids
   were equidistant, `both NULL` means nothing was within range. §6.1 names only
   the second case; the first needs telling apart from it, since one is a
   generation failure and the other is a collision in the id space.

`θ` defaults to 0.8, per open question §9.2 — a starting point with no
principled basis, tunable with `--theta`.

## Tests

```sh
.venv/bin/python -m pytest
```

74 tests. Expected values are derived independently of the implementation
(`tests/reference.py` re-derives pool membership, latest-wins resolution, heads
and Levenshtein distance from the spec text and imports nothing from `seshat`),
and the load-bearing assertions have been checked by mutating the
implementation to confirm they turn red.

## Not yet built

- **Embeddings and hybrid retrieval** [§6, §6.3]. `context` scores are already
  in RRF shape (`1/(k+rank)`, k=60), so adding the vector side changes scores,
  not signatures. The async write path is the constraint that matters:
  `note()` must never block on Ollama.
- **The consistency checker** [§7]. A CLI subcommand, not a tool.
- **Growth management** [§8] — unsolved in the spec, deliberately not guessed at
  here.

## License

The spec's preamble records an intent of MIT. No LICENSE file is included yet;
add one when the project is actually released.
