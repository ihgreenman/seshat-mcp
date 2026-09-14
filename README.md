# seshat-mcp

A persistent note store an LLM assistant can query and write cheaply.
Design and rationale: [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md).

**Built against spec version 1.1.** The version is declared in the spec header
and recorded as `seshat.SPEC_VERSION`; `help` and `seshat info` report it
alongside the software and store versions, which move independently (§11.1).
`tests/test_spec_version.py` fails if the code ever claims a revision the
document has not reached.

**Status.** The full 1.1 tool surface is implemented, and retrieval is hybrid:
FTS5/BM25 and vector KNN fused with RRF. Snapshot capture stores raw bytes
without extraction. Remaining gaps are reported by `help` rather than hidden —
`snapshot_extraction: false`, and `vector: false` whenever the vector side is
not actually available. See [Not yet built](#not-yet-built).

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python ≥ 3.11. Runtime dependencies: `mcp`, `pydantic`, and optionally
`sqlite-vec` for vector search (`pip install -e '.[vector]'`). Nothing else —
the store is stdlib `sqlite3`, both HTTP clients are stdlib `urllib`, and the
BIP-39 wordlist is vendored.

Vector search additionally wants [Ollama](https://ollama.com) with
`ollama pull nomic-embed-text`. Both are optional at runtime: without either,
retrieval falls back to keyword search and says so.

## Run

```sh
seshat --db ~/notes/seshat.db serve          # MCP server on stdio
seshat --db ~/notes/seshat.db --no-snapshots --no-embeddings serve
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
seshat embed                # embed everything outstanding, now
seshat reembed              # discard and rebuild all embeddings (model migration)
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

## Retrieval

FTS5/BM25 and vector KNN run independently and fuse with reciprocal rank
fusion, `score = Σ 1/(k + rankᵢ)` with k=60 (§6). RRF is rank-based, so BM25
scores and cosine distances never have to be made commensurable — there is no
invented calibration anywhere in the path.

Sum, not maximum: a note both retrievers rank second beats a note one ranks
first, which is the entire reason for running two of them.

**Degradation is a first-class path, not an error** (§6.3). No `sqlite-vec`, no
Ollama, a cold model, or simply nothing embedded yet all produce the FTS
ranking alone — same score shape, so a caller's triage stays calibrated — and
`help` reports `vector: false` so the degradation is visible rather than
silently worse. A failed query embedding also puts the vector side on a 30s
cooldown, without which every subsequent `context` would pay the timeout while
Ollama is down.

Embedding is asynchronous and off the write path (§6.3): `note()` commits and
returns, a worker catches up. A note is keyword-findable immediately and
semantically findable seconds later. **Unembedded is a legitimate state**, and
the backlog is a query — a row absent from `embedding_meta`, or carrying a
different `model`, needs embedding — which is what makes model migration a
`DELETE` plus a drain rather than a schema change.

The nomic task prefixes (`search_document:` / `search_query:`) are applied by
two module-level functions and **a backend cannot apply them at all**: an
`Embedder` implements only `embed(texts)` and receives fully-prefixed text.
§6 warns the convention is easy to get wrong in exactly one code path, so there
is exactly one code path.

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
   quoted, so no caller string reaches FTS5's query syntax. Function words are
   dropped from the expansion, for a reason found the hard way: see below.
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

### One defect found by running it, and the fix

Live hybrid retrieval on a five-note corpus ranked *"Catastrophic cancellation
in tail energy"* first for the query **"combining two rankings"**, above the
note actually about rank fusion — which the vector side had correctly ranked
first.

The cause is an interaction, not a bug in any one part. `OR` expansion lets a
single token retrieve a document, the only matching token was **"two"**, and RRF
is rank-blind: it cannot see that the keyword match was worthless, so a spurious
rank-1 hit contributed exactly as much as a good one, and corroboration across
the two retrievers then promoted the wrong note above the right one.

The fix is at the source — function words are dropped from the FTS expansion,
keeping them only when a query consists of nothing else (so "one two three"
still finds the note that says it). All four probe queries rank correctly after
it. Worth stating plainly: this was found on a corpus of five notes, so the
*frequency* of the failure is unmeasured. The mechanism, though, is general —
function word plus OR plus rank-blind fusion — and does not depend on corpus
size.

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

154 tests, no network access — the embedder and the fetcher are both injected,
and the server tests run the subprocess with `--no-snapshots --no-embeddings`
so nothing in the suite reaches Ollama or the web. Expected values are derived independently of the
implementation (`tests/reference.py` re-derives pool membership, latest-wins
resolution, heads and Levenshtein distance from the spec text and imports
nothing from `seshat`), and the load-bearing assertions have been checked by
mutating the implementation to confirm they turn red — including the pool join,
the max/min rule, truncation counts, silent id resolution, the store lock,
stdout discipline, snapshot immutability, write-path isolation, the task
prefixes, strip-vs-index, vector degradation, and sum-vs-max fusion.

## Not yet built

- **Snapshot extraction** [§6.6] — the `thin` status cannot be assessed until
  an extractor exists, and `help` reports `snapshot_extraction: false` rather
  than letting `snapshots: true` imply it.
- **The consistency checker** [§7] — increment 3, a CLI subcommand.
- **The review interface** [§10] — increment 4.
- **Growth management** [§8] — unsolved in the spec, deliberately not guessed
  at here.

## License

The spec is © 2026 Ian Greenhoe, MIT. See `CREDITS.md` for third-party material.
